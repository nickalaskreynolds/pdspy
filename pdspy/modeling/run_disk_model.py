#!/usr/bin/env python3

from ..constants.astronomy import arcsec
from .YSOModel import YSOModel
from .. import interferometry as uv
from .. import misc
from .. import dust
import scipy.signal
import subprocess
import argparse
import signal
import numpy
import time
import os
from mpi4py import MPI

import sys
if sys.version_info.major > 2:
    from subprocess import TimeoutExpired
else:
    from subprocess32 import TimeoutExpired

comm = MPI.COMM_WORLD

################################################################################
#
# Create a function which returns a model of the data.
#
################################################################################

def run_disk_model(visibilities, images, spectra, params, parameters, \
        plot=False, ncpus=1, ncpus_highmass=1, with_hyperion=False, \
        timelimit=3600, source="disk", nice=None, disk_vis=False, \
        no_radiative_transfer=False):

    # Set the values of all of the parameters.

    p = {}
    for key in parameters:
        if parameters[key]["fixed"]:
            if parameters[key]["value"] in parameters.keys():
                if parameters[parameters[key]["value"]]["fixed"]:
                    value = parameters[parameters[key]["value"]]["value"]
                else:
                    value = params[parameters[key]["value"]]
            else:
                value = parameters[key]["value"]
        else:
            value = params[key]

        if key[0:3] == "log":
            p[key[3:]] = 10.**value
        else:
            p[key] = value

    # Make sure alpha is defined.

    p["alpha"] = p["gamma"] + p["beta"]
    p["alpha_large"] = p["gamma"] + p["beta_large"]

    # If we're using a Pringle disk, make sure the scale height is set correctly

    if p["disk_type"] in ["exptaper","settledexptaper"]:
        p["h_0"] *= p["R_disk"]**p["beta"]

    # Get the needed values of the gaps.

    p["R_in_gap1"] = p["R_gap1"] - p["w_gap1"]/2
    p["R_out_gap1"] = p["R_gap1"] + p["w_gap1"]/2
    p["R_in_gap2"] = p["R_gap2"] - p["w_gap2"]/2
    p["R_out_gap2"] = p["R_gap2"] + p["w_gap2"]/2
    p["R_in_gap3"] = p["R_gap3"] - p["w_gap3"]/2
    p["R_out_gap3"] = p["R_gap3"] + p["w_gap3"]/2

    # Set up the dust.

    dustopac = p["dust_file"]
    dust_gen = dust.DustGenerator(dust.__path__[0]+"/data/"+dustopac)
    if not p["disk_type"] in ["settled","settledexptaper"]:
        ddust = dust_gen(p["a_max"] / 1e4, p["p"])

    dustopac_env = p["envelope_dust"]
    env_dust_gen = dust.DustGenerator(dust.__path__[0]+"/data/"+dustopac_env)
    edust = env_dust_gen(1.0e-4, 3.5)

    # Make sure we are in a temp directory to not overwrite anything.

    original_dir = os.environ["PWD"]
    os.mkdir("/tmp/temp_{1:s}_{0:d}".format(comm.Get_rank(), source))
    os.chdir("/tmp/temp_{1:s}_{0:d}".format(comm.Get_rank(), source))

    # Write the parameters to a text file so it is easy to keep track of them.

    f = open("params.txt","w")
    for key in p:
        f.write("{0:s} = {1}\n".format(key, p[key]))
    f.close()

    # Set up the model and run the thermal simulation.

    if p["M_disk"] > 0.001 or p["R_disk"] < 50 or p["M_env"] > 0.001 or \
            p["R_env"] < 500 or p["h_0"] > 0.25:
        if with_hyperion:
            nphi = 201
            code = "hyperion"
            nprocesses = ncpus_highmass
        else:
            nphi = 101
            code = "radmc3d"
            nprocesses = ncpus_highmass
    else:
        nphi = 101
        code = "radmc3d"
        nprocesses = ncpus

    m = YSOModel()
    m.add_star(mass=p["M_star"],luminosity=p["L_star"],temperature=p["T_star"])
    m.set_spherical_grid(p["R_in"], p["R_env"], 100, nphi, 2, code=code)

    if p["disk_type"] == "exptaper":
        m.add_pringle_disk(mass=p["M_disk"]*p["f_M_large"], rmin=p["R_in"], \
                rmax=p["R_disk"], plrho=p["alpha_large"], \
                h0=p["h_0"]*p["f_h_large"], plh=p["beta_large"], dust=ddust, \
                gap_rin=[p["R_in"],p["R_in_gap1"],p["R_in_gap2"],\
                p["R_in_gap3"]], gap_rout=[p["R_cav"],p["R_out_gap1"],\
                p["R_out_gap2"],p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                p["delta_gap1"],p["delta_gap2"],p["delta_gap3"]])
        if p["f_M_large"] < 1:
            m.add_pringle_disk(mass=p["M_disk"]*(1-p["f_M_large"]), \
                    rmin=p["R_in"], rmax=p["R_disk"], plrho=p["alpha"], \
                    h0=p["h_0"], plh=p["beta"], dust=edust, gap_rin=[p["R_in"],\
                    p["R_in_gap1"],p["R_in_gap2"],p["R_in_gap3"]], \
                    gap_rout=[p["R_cav"],p["R_out_gap1"],p["R_out_gap2"],\
                    p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                    p["delta_gap1"], p["delta_gap2"],p["delta_gap3"]])
    elif p["disk_type"] == "settled":
        m.add_settled_disk(mass=p["M_disk"], rmin=p["R_in"], rmax=p["R_disk"], \
                plrho=p["alpha"], h0=p["h_0"], plh=p["beta"], dust=dust_gen,\
                gap_rin=[p["R_in"],p["R_in_gap1"],p["R_in_gap2"],\
                p["R_in_gap3"]], gap_rout=[p["R_cav"],p["R_out_gap1"],\
                p["R_out_gap2"],p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                p["delta_gap1"],p["delta_gap2"],p["delta_gap3"]], \
                amin=p["a_min"], amax=p["a_max"], pla=p["p"], na=p["na"], \
                alpha_settle=p["alpha_settle"])
    elif p["disk_type"] == "settledexptaper":
        m.add_settled_pringle_disk(mass=p["M_disk"], rmin=p["R_in"], \
                rmax=p["R_disk"], plrho=p["alpha"], h0=p["h_0"], plh=p["beta"],\
                dust=dust_gen,gap_rin=[p["R_in"],p["R_in_gap1"],p["R_in_gap2"],\
                p["R_in_gap3"]], gap_rout=[p["R_cav"],p["R_out_gap1"],\
                p["R_out_gap2"],p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                p["delta_gap1"],p["delta_gap2"],p["delta_gap3"]], \
                amin=p["a_min"], amax=p["a_max"], pla=p["p"], na=p["na"], \
                alpha_settle=p["alpha_settle"])
    else:
        m.add_disk(mass=p["M_disk"]*p["f_M_large"], rmin=p["R_in"], \
                rmax=p["R_disk"], plrho=p["alpha_large"], \
                h0=p["h_0"]*p["f_h_large"], plh=p["beta_large"], dust=ddust, \
                gap_rin=[p["R_in"],p["R_in_gap1"],p["R_in_gap2"],\
                p["R_in_gap3"]], gap_rout=[p["R_cav"],p["R_out_gap1"],\
                p["R_out_gap2"],p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                p["delta_gap1"],p["delta_gap2"],p["delta_gap3"]])
        if p["f_M_large"] < 1:
            m.add_disk(mass=p["M_disk"]*(1-p["f_M_large"]), rmin=p["R_in"], \
                    rmax=p["R_disk"], plrho=p["alpha"], h0=p["h_0"], \
                    plh=p["beta"], dust=edust, gap_rin=[p["R_in"],\
                    p["R_in_gap1"],p["R_in_gap2"],p["R_in_gap3"]], \
                    gap_rout=[p["R_cav"],p["R_out_gap1"],p["R_out_gap2"],\
                    p["R_out_gap3"]], gap_delta=[p["delta_cav"],\
                    p["delta_gap1"], p["delta_gap2"],p["delta_gap3"]])

    if p["envelope_type"] == "ulrich":
        m.add_ulrich_envelope(mass=p["M_env"], rmin=p["R_in"], rmax=p["R_env"],\
                cavpl=p["ksi"], cavrfact=p["f_cav"], dust=edust)
    else:
        pass

    m.grid.set_wavelength_grid(0.1,1.0e5,500,log=True)

    # If we just want to generate the model, quit here.

    if no_radiative_transfer:
        os.system("rm params.txt")
        os.chdir(original_dir)
        os.rmdir("/tmp/temp_{1:s}_{0:d}".format(comm.Get_rank(), source))

        return m

    # Run the thermal simulation.

    if code == "hyperion":
        m.run_thermal(code="hyperion", nphot=2e5, mrw=True, pda=True, \
                niterations=20, mpi=True, nprocesses=nprocesses, verbose=False)

        # Convert model to radmc-3d format.

        m.make_hyperion_symmetric()

        m.convert_hyperion_to_radmc3d()
    else:
        try:
            t1 = time.time()
            m.run_thermal(code="radmc3d", nphot=1e6, modified_random_walk=True,\
                    mrw_gamma=2, mrw_tauthres=10, mrw_count_trigger=100, \
                    verbose=False, setthreads=nprocesses, \
                    timelimit=timelimit, nice=nice)
            t2 = time.time()
            f = open(original_dir + "/times.txt", "a")
            f.write("{0:f}\n".format(t2-t1))
            f.close()
        # Catch a timeout error from models running for too long...
        except TimeoutExpired:
            t2 = time.time()
            f = open(original_dir + "/times.txt", "a")
            f.write("{0:f}\n".format(t2-t1))
            f.close()

            os.system("mv params.txt {0:s}/params_timeout_{1:s}".format(\
                    original_dir, time.strftime("%Y-%m-%d-%H:%M:%S", \
                    time.gmtime())))
            os.system("rm *.inp *.out *.dat *.uinp")
            os.chdir(original_dir)
            os.rmdir("/tmp/temp_{1:s}_{0:d}".format(comm.Get_rank(), source))

            return 0.
        # Catch a strange RADMC3D error and re-run. Not an ideal fix, but 
        # perhaps the simplest option.
        except:
            t1 = time.time()
            m.run_thermal(code="radmc3d", nphot=1e6, modified_random_walk=True,\
                    mrw_gamma=2, mrw_tauthres=10, mrw_count_trigger=100, \
                    verbose=False, setthreads=nprocesses, \
                    timelimit=timelimit, nice=nice)
            t2 = time.time()
            f = open(original_dir + "/times.txt", "a")
            f.write("{0:f}\n".format(t2-t1))
            f.close()

    # Run the images/visibilities/SEDs. If plot == "concat" then we are doing
    # a fit and we need less. Otherwise we are making a plot of the best fit 
    # model so we need to generate a few extra things.

    # Run the visibilities.

    for j in range(len(visibilities["file"])):
        m.run_image(name=visibilities["lam"][j], nphot=1e5, \
                npix=visibilities["npix"][j], \
                pixelsize=visibilities["pixelsize"][j], \
                lam=visibilities["lam"][j], incl=p["i"], \
                pa=p["pa"], dpc=p["dpc"], code="radmc3d", \
                mc_scat_maxtauabs=5, verbose=False, setthreads=nprocesses, \
                writeimage_unformatted=True, nice=nice)

        m.images[visibilities["lam"][j]].image *= p["flux_unc{0:d}".format(j+1)]

        m.visibilities[visibilities["lam"][j]] = uv.interpolate_model(\
                visibilities["data"][j].u, visibilities["data"][j].v, \
                visibilities["data"][j].freq, m.images[visibilities["lam"][j]],\
                dRA=-p["x0"], dDec=-p["y0"], nthreads=nprocesses)

        # Run a high resolution image.

        m.run_image(name=visibilities["lam"][j]+"high", nphot=1e5, \
                npix=256, \
                pixelsize=0.02, \
                lam=visibilities["lam"][j], incl=p["i"], \
                pa=p["pa"], dpc=p["dpc"], code="radmc3d", \
                mc_scat_maxtauabs=5, verbose=False, setthreads=nprocesses, \
                writeimage_unformatted=True, nice=nice)

        # Run a low resolution image

        m.run_image(name=visibilities["lam"][j]+"low", nphot=1e5, \
                npix=256, \
                pixelsize=0.2, \
                lam=visibilities["lam"][j], incl=p["i"], \
                pa=p["pa"], dpc=p["dpc"], code="radmc3d", \
                mc_scat_maxtauabs=5, verbose=False, setthreads=nprocesses, \
                writeimage_unformatted=True, nice=nice)

        # Combine into one visibility dataset.

        m.visibilities[visibilities["lam"][j]+"high"] = uv.interpolate_model(\
                visibilities["data"][j].u, visibilities["data"][j].v, \
                visibilities["data"][j].freq, m.images[visibilities["lam"][j]+\
                "high"], dRA=-p["x0"], dDec=-p["y0"], nthreads=nprocesses)

        low = visibilities["data"][j].uvdist < 2 / (256*0.02*arcsec)

        m.visibilities[visibilities["lam"][j]+"low"] = uv.interpolate_model(\
                visibilities["data"][j].u[low], visibilities["data"][j].v[low],\
                visibilities["data"][j].freq, m.images[visibilities["lam"][j]+\
                "low"], dRA=-p["x0"], dDec=-p["y0"], nthreads=nprocesses)

        # Combine the two.

        m.visibilities[visibilities["lam"][j]+"test"] = uv.Visibilities(\
                m.visibilities[visibilities["lam"][j]+"high"].u, \
                m.visibilities[visibilities["lam"][j]+"high"].v, \
                visibilities["data"][j].freq, \
                m.visibilities[visibilities["lam"][j]+"high"].real.copy(), \
                m.visibilities[visibilities["lam"][j]+"high"].imag.copy(), \
                m.visibilities[visibilities["lam"][j]+"high"].weights)

        m.visibilities[visibilities["lam"][j]+"test"].real[low] = \
                m.visibilities[visibilities["lam"][j]+"low"].real.copy()
        m.visibilities[visibilities["lam"][j]+"test"].imag[low] = \
                m.visibilities[visibilities["lam"][j]+"low"].imag.copy()


        if plot:
            # Make high resolution visibilities. 

            u, v = numpy.meshgrid(numpy.linspace(-2.0e6, 2.0e6, 2000), \
                    numpy.linspace(-2.0e6, 2.0e6, 2000))
            u, v = u.reshape((u.size,)), v.reshape((v.size,))

            m.visibilities[visibilities["lam"][j]+"_high"] = \
                    uv.interpolate_model(u, v, visibilities["data"][j].freq, \
                    m.images[visibilities["lam"][j]], dRA=-p["x0"], \
                    dDec=-p["y0"], nthreads=nprocesses)

            m.visibilities[visibilities["lam"][j]+"high"] = \
                    uv.interpolate_model(u, v, visibilities["data"][j].freq, \
                    m.images[visibilities["lam"][j]+"high"], dRA=-p["x0"], \
                    dDec=-p["y0"], nthreads=nprocesses)

            low = m.visibilities[visibilities["lam"][j]+"high"].uvdist < \
                    2 / (256*0.02*arcsec)

            m.visibilities[visibilities["lam"][j]+"low"] = \
                    uv.interpolate_model(u[low], v[low], \
                    visibilities["data"][j].freq, \
                    m.images[visibilities["lam"][j]+"low"], dRA=-p["x0"], \
                    dDec=-p["y0"], nthreads=nprocesses)

            m.visibilities[visibilities["lam"][j]+"test"] = \
                    uv.Visibilities(u, v, visibilities["data"][j].freq, \
                    m.visibilities[visibilities["lam"][j]+"high"].real.copy(), \
                    m.visibilities[visibilities["lam"][j]+"high"].imag.copy(), \
                    m.visibilities[visibilities["lam"][j]+"high"].weights)

            m.visibilities[visibilities["lam"][j]+"test"].real[low] = \
                    m.visibilities[visibilities["lam"][j]+"low"].real.copy()
            m.visibilities[visibilities["lam"][j]+"test"].imag[low] = \
                    m.visibilities[visibilities["lam"][j]+"low"].imag.copy()

            m.visibilities[visibilities["lam"][j]+"diff"] = \
                    uv.Visibilities(u, v, visibilities["data"][j].freq, \
                    m.visibilities[visibilities["lam"][j]+"test"].real - \
                    m.visibilities[visibilities["lam"][j]+"_high"].real, \
                    m.visibilities[visibilities["lam"][j]+"test"].imag - \
                    m.visibilities[visibilities["lam"][j]+"_high"].imag, \
                    m.visibilities[visibilities["lam"][j]+"test"].weights)

            # Run the 2D visibilities.

            m.visibilities[visibilities["lam"][j]+"_2d"] = \
                    uv.interpolate_model(visibilities["data2d"][j].u, \
                    visibilities["data2d"][j].v, \
                    visibilities["data2d"][j].freq, \
                    m.images[visibilities["lam"][j]], dRA=-p["x0"], \
                    dDec=-p["y0"], nthreads=nprocesses)

            # Run a millimeter image.

            m.run_image(name=visibilities["lam"][j], nphot=1e5, \
                    npix=visibilities["image_npix"][j], \
                    pixelsize=visibilities["image_pixelsize"][j], \
                    lam=visibilities["lam"][j], incl=p["i"], \
                    pa=-p["pa"], dpc=p["dpc"], code="radmc3d", \
                    mc_scat_maxtauabs=5, verbose=False, setthreads=nprocesses, \
                    nice=nice)

            x, y = numpy.meshgrid(numpy.linspace(-256,255,512), \
                    numpy.linspace(-256,255,512))

            beam = misc.gaussian2d(x, y, 0., 0., \
                    visibilities["image"][j].header["BMAJ"]/2.355/\
                    visibilities["image"][j].header["CDELT2"], \
                    visibilities["image"][j].header["BMIN"]/2.355/\
                    visibilities["image"][j].header["CDELT2"], \
                    (90-visibilities["image"][j].header["BPA"])*numpy.pi/180., \
                    1.0)

            m.images[visibilities["lam"][j]].image = scipy.signal.fftconvolve(\
                    m.images[visibilities["lam"][j]].image[:,:,0,0], beam, \
                    mode="same").reshape(m.images[visibilities["lam"][j]].\
                    image.shape)

            # Run visibilities that include only the contribution of the disk.

            if disk_vis and parameters["envelope_type"]["value"] == "ulrich":
                density_original = m.grid.density.copy()
                temperature_original = m.grid.temperature.copy()
                dust_original = m.grid.dust.copy()

                del m.grid.density[-1]
                del m.grid.temperature[-1]
                del m.grid.dust[-1]

                m.run_visibilities(name=visibilities["lam"][j]+"_disk", \
                        nphot=1e5, npix=2048, pixelsize=0.05, \
                        lam=visibilities["lam"][j], incl=p["i"], pa=p["pa"], \
                        dpc=p["dpc"], code="radmc3d", mc_scat_maxtauabs=5, \
                        verbose=False, setthreads=nprocesses, nice=nice)

                m.grid.density = density_original
                m.grid.temperature = temperature_original
                m.grid.dust = dust_original

    # Run the images.

    for j in range(len(images["file"])):
        m.run_image(name=images["lam"][j], nphot=1e5, \
                npix=images["npix"][j], pixelsize=images["pixelsize"][j], \
                lam=images["lam"][j], incl=p["i"], \
                pa=p["pa"], dpc=p["dpc"], code="radmc3d", \
                mc_scat_maxtauabs=5, verbose=False, setthreads=nprocesses, \
                nice=nice)

        # Convolve with the beam.

        x, y = numpy.meshgrid(numpy.linspace(-256,255,512), \
                numpy.linspace(-256,255,512))

        beam = misc.gaussian2d(x, y, 0., 0., images["bmaj"][j]/2.355/\
                images["pixelsize"][j], images["bmin"][j]/2.355/\
                image["pixelsize"][j], (90-images["bpa"][j])*numpy.pi/180., 1.0)

        m.images[images["lam"][j]].image = scipy.signal.fftconvolve(\
                m.images[images["lam"][j]].image[:,:,0,0], beam, mode="same").\
                reshape(m.images[images["lam"][j]].image.shape)

    # Run the SED.

    if "total" in spectra:
        if plot:
            m.set_camera_wavelength(numpy.logspace(-1,4,50))
        else:
            m.set_camera_wavelength(spectra["total"].wave)

        m.run_sed(name="SED", nphot=1e4, loadlambda=True, incl=p["i"],\
                pa=p["pa"], dpc=p["dpc"], code="radmc3d", \
                camera_scatsrc_allfreq=True, mc_scat_maxtauabs=5, \
                verbose=False, setthreads=nprocesses, nice=nice)

        # Redden the SED based on the reddening.

        m.spectra["SED"].flux = dust.redden(m.spectra["SED"].wave, \
                m.spectra["SED"].flux, p["Ak"], law="mcclure")

        # Now take the log of the SED.

        if not plot:
            m.spectra["SED"].flux = numpy.log10(m.spectra["SED"].flux)

    # Clean up everything and return.

    os.system("rm params.txt")
    os.chdir(original_dir)
    os.rmdir("/tmp/temp_{1:s}_{0:d}".format(comm.Get_rank(), source))

    return m
