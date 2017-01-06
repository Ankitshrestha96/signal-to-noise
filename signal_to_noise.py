import math
import numpy as np
from scipy.interpolate import interp1d
from astropy import units as u
from astropy import constants as c
from astropy.table import Table

def ensure_unit(arg, unit):
    if not isinstance(arg, u.Quantity):
        arg *= unit
    return arg.to(unit)

class Optic:
    def __init__(self, aperture, focal_length, throughput_filename, central_obstruction = 0 * u.mm):
        """
        Class representing imager optics (e.g. Canon lens, RASA telescope),
        incorporates basic attributes such as aperture diameter, focal length
        and central obstruction.
        """
        self.aperture = ensure_unit(aperture, u.nm)
        self.central_obstruction = ensure_unit(central_obstruction, u.mm)
        
        self.aperture_area = np.pi * (self.aperture**2 - self.central_obstruction**2).to(u.m**2) / 4
        
        self.focal_length = ensure_unit(focal_length, u.mm)
        
        tau_data = Table.read(throughput_filename)
        
        if not tau_data['Wavelength'].unit:
            tau_data['Wavelength'].unit = u.nm
        self.wavelengths = tau_data['Wavelength'].quantity.to(u.nm)
        
        if not tau_data['Throughput'].unit:
            tau_data['Throughput'].unit = u.dimensionless_unscaled
        self.throughput = tau_data['Throughput'].quantity.to(u.dimensionless_unscaled)

class Camera:
    def __init__(self, pixel_size, resolution, read_noise, dark_current, QE_filename):
        """
        Class representing a camera, incorporated basic properties such
        as pixel size, resolution, read noise and dark current.
        """
        self.pixel_size = ensure_unit(pixel_size, u.micron / u.pixel)
        self.resolution = ensure_unit(resolution, u.pixel)
        self.read_noise = ensure_unit(read_noise, u.electron / u.pixel)
        self.dark_current = ensure_unit(dark_current, u.electron / (u.second * u.pixel))
        
        QE_data = Table.read(QE_filename)
        
        if not QE_data['Wavelength'].unit:
            QE_data['Wavelength'].unit = u.nm      
        self.wavelengths = QE_data['Wavelength'].quantity.to(u.nm)
        
        if not QE_data['QE'].unit:
            QE_data['QE'].unit = u.electron / u.photon
        self.QE = QE_data['QE'].quantity.to(u.electron / u.photon)

class Filter:
    def __init__(self, transmission_filename, sky_mu):
        """
        Class representing a simple bandpass filter, assumed to be a perfect 'top hat' 
        defined by a pair of wavelengths. Also incorporates sky surface brightness for
        the filter band.
        """
        transmission_data = Table.read(transmission_filename)
        
        if not transmission_data['Wavelength'].unit:
            transmission_data['Wavelength'].unit = u.nm
        self.wavelengths = transmission_data['Wavelength'].quantity.to(u.nm)
        
        if not transmission_data['Transmission'].unit:
            transmission_data['Transmission'].unit = u.dimensionless_unscaled
        self.transmission = transmission_data['Transmission'].quantity.to(u.dimensionless_unscaled)
        
        self.sky_mu = ensure_unit(sky_mu, u.ABmag)

class Imager:
    def __init__(self, optic, camera, band):
        if not isinstance(optic, Optic):
            raise ValueError("optic must be an instance of the Optic class")
        if not isinstance(camera, Camera):
            raise ValueError("camera must be an instance of the Camera class")
        if not isinstance(band, Filter):
            raise ValueError("band must be an instance of the Filter class")
            
        self.optic = optic
        self.camera = camera
        self.band = band
        
        # Calculate pixel scale, area
        self.pixel_scale = (self.camera.pixel_size / self.optic.focal_length)
        self.pixel_scale = self.pixel_scale.to(u.arcsecond/u.pixel, \
                                               equivalencies = u.equivalencies.dimensionless_angles())
        self.pixel_area = self.pixel_scale**2 * u.pixel
        
        # Calculate end to end efficiencies, etc.
        self._efficiencies()
        
        # Calculate sky count rate for later use
        self.sky_rate = self.SB_to_rate(self.band.sky_mu)
        
        # Calculate gamma0
        self._gamma0()
    
    def SB_snr(self, signal_SB, total_exp_time, sub_exp_time=300 * u.second, binning=1, N=1):
    
        # Signal count rates
        signal_rate = self.SB_to_rate(signal_SB) # e/pixel/second
    
        # Number of sub-exposures
        total_exp_time = ensure_unit(total_exp_time, u.second)
        sub_exp_time = ensure_unit(sub_exp_time, u.second)
        
        number_subs = int(math.ceil(total_exp_time/sub_exp_time))
        
        if (total_exp_time != number_subs * sub_exp_time):
            total_exp_time = number_subs * sub_exp_time
            print('Rounding up total exposure time to next integer multiple of sub-exposure time:', total_exp_time)
    
        # Noise sources
        signal = (signal_rate * total_exp_time).to(u.electron / u.pixel)
        sky_counts = (self.sky_rate * total_exp_time).to(u.electron / u.pixel)
        dark_counts = (self.camera.dark_current * total_exp_time).to(u.electron / u.pixel)
        total_read_noise = ((number_subs)**0.5 * self.camera.read_noise).to(u.electron / u.pixel)
    
        noise = (signal.value + sky_counts.value + dark_counts.value + total_read_noise.value**2)**0.5
        noise *= u.electron / u.pixel
    
        snr = (N * binning)**0.5 * signal/noise # Number of optics in array or pixel binning increases snr as n^0.5
        
        return snr.to(u.dimensionless_unscaled)
    
    def SB_etc(self, signal_SB, snr_target, sub_exp_time=300 * u.second, binning=1, N=1):
    
        # Science count rates
        signal_rate = self.SB_to_rate(signal_SB)
            
        # Convert target SNR per array combined, binned pixel to SNR per unbinned pixel
        snr_target /= (N * binning)**0.5
        snr_target = ensure_unit(snr_target, u.dimensionless_unscaled)
    
        sub_exp_time = ensure_unit(sub_exp_time, u.second)

        # If required total exposure time is much greater than the length of a sub-exposure then
        # all noise sources (including read noise) are proportional to t^0.5 and we can use a 
        # simplified expression to estimate total exposure time.
        noise_squared_rate = (signal_rate.value + self.sky_rate.value + self.camera.dark_current.value + \
                              self.camera.read_noise.value**2 / sub_exp_time.value)
        noise_squared_rate *= u.electron**2 / (u.pixel**2 * u.second)
        total_exp_time = (snr_target**2 * noise_squared_rate / signal_rate**2).to(u.second)
    
        # The simplified expression underestimates read noise due to fractional number of sub-exposure,
        # the effect will be neglible unless the total exposure time is very short but we can fix it anyway...
        # First round up to the next integer number of sub-exposures:
        number_subs = int(math.ceil(total_exp_time / sub_exp_time))
        # If the SNR has dropped below the target value as a result of the extra read noise add another sub
        # Note: calling snr() here is horribly inefficient as it recalculates a bunch of stuff but I don't care.
        while self.SB_snr(signal_SB, number_subs*sub_exp_time, sub_exp_time, binning, N) < snr_target:
            print("Adding a sub-exposure to overcome read noise!")
            number_subs += 1
    
        return number_subs*sub_exp_time, number_subs
    
    def SB_limit(self, total_exp_time, snr_target, snr_calculation='per pixel', sub_exp_time=600, binning=1, N=1, \
                 enable_read_noise=True, enable_sky_noise=True, enable_dark_noise=True):
        snr_target /= (N * binning)**0.5
        # Convert target SNR per array combined, binned pixel to SNR per unbinned pixel
        if snr_calculation == 'per pixel':
            pass
        elif snr_calculation == 'per arcseconds squared':
            snr_target *= self.pixel_scale/(u.arcsecond/u.pixel)
        else:
            raise ValueError('invalid snr target type {}'.format(snr_calculation))
            
        snr_target = ensure_unit(snr_target, u.dimensionless_unscaled)
    
        # Number of sub-exposures
        total_exp_time = ensure_unit(total_exp_time, u.second)
        sub_exp_time = ensure_unit(sub_exp_time, u.second)
        
        number_subs = int(math.ceil(total_exp_time/sub_exp_time))
        
        if (total_exp_time != number_subs * sub_exp_time):
            total_exp_time = number_subs * sub_exp_time
            print('Rounding up total exposure time to next integer multiple of sub-exposure time:', total_exp_time)
        
        # Noise sources
        sky_counts = self.sky_rate * total_exp_time if enable_sky_noise else 0.0 * u.electron / u.pixel
        dark_counts = self.camera.dark_current * total_exp_time if enable_dark_noise else 0.0 * u.electron / u.pixel
        total_read_noise = math.sqrt(number_subs) * self.camera.read_noise if enable_read_noise else 0.0 * u.electron / u.pixel
    
        noise_squared = (sky_counts.value + dark_counts.value + total_read_noise.value**2) * u.electron**2 / u.pixel**2
    
        # Calculate science count rate for target signal to noise ratio
        a = (total_exp_time**2).value
        b = -((snr_target)**2 * total_exp_time).value
        c = -((snr_target)**2 * noise_squared).value
        
        signal_rate = (-b + np.sqrt(b**2 - 4*a*c))/(2*a) *  u.electron / (u.pixel * u.second)
        
        return self.rate_to_SB(signal_rate)

    def ABmag_to_rate(self, mag):
        mag = ensure_unit(mag, u.ABmag)
        
        f_nu = mag.to(u.W / (u.m**2 * u.Hz), equivalencies=u.equivalencies.spectral_density(self.pivot_wave))
        rate = f_nu * self.optic.aperture_area * self._iminus1 * u.photon / c.h
        
        return rate.to(u.electron / u.second)
    
    def rate_to_ABmag(self, rate):
        ensure_unit(rate, u.electron / u.second)
        
        f_nu = rate * c.h / (self.optic.aperture_area * self._iminus1 * u.photon)
        return f_nu.to(u.ABmag, equivalencies=u.equivalencies.spectral_density(self.pivot_wave))
        
    def SB_to_rate(self, mag):
        SB_rate = self.ABmag_to_rate(mag) * self.pixel_area / (u.arcsecond**2)
        return SB_rate.to(u.electron / (u.second * u.pixel))
    
    def rate_to_SB(self, SB_rate):
        ensure_unit(SB_rate, u.electron / (u.second * u.pixel))
        
        rate = SB_rate * u.arcsecond**2 / self.pixel_area
        return self.rate_to_ABmag(rate)

    def ABmag_to_flux(self, mag):
        mag = ensure_unit(mag, u.ABmag)
        
        f_nu = mag.to(u.W / (u.m**2 * u.Hz), equivalencies=u.equivalencies.spectral_density(self.pivot_wave))
        flux = f_nu * c.c * self._iminus2 * u.photon / u.electron
        
        return flux.to(u.W / (u.m**2))
    
    def pointsource_snr(self, signal_mag, total_exp_time, sub_exp_time=300 * u.second, binning=1, N=1):
        signal_mag *= u.ABmag
        signal_rate = self.ABmag_to_rate(signal_mag) / (6.7810449442290848 * u.pixel)
        signal_SB = self.rate_to_SB(signal_rate)
        return self.SB_snr(signal_SB.value, total_exp_time, sub_exp_time, binning, N)
        
    def pointsource_etc(self, signal_mag, snr_target, sub_exp_time=300 * u.second, binning=1, N=1):
        signal_mag *= u.ABmag
        signal_rate = self.ABmag_to_rate(signal_mag) / (6.7810449442290848 * u.pixel)
        signal_SB = self.rate_to_SB(signal_rate)
        return self.SB_etc(signal_SB.value, snr_target, sub_exp_time, binning, N)
    
    def pointsource_limit(self, total_exp_time, snr_target, snr_calculation='per pixel', sub_exp_time=600, \
                          binning=1, N=1, enable_read_noise=True, enable_sky_noise=True, enable_dark_noise=True):
        signal_SB = self.SB_limit(total_exp_time, snr_target, snr_calculation, sub_exp_time, binning, N, \
                 enable_read_noise, enable_sky_noise, enable_dark_noise) 
        signal_rate = self.SB_to_rate(signal_SB) * (6.7810449442290848 * u.pixel)
        return self.rate_to_ABmag(signal_rate)
    
    def _efficiencies(self):
        # Fine wavelength grid spanning range of filter transmission profile
        waves = np.arange(self.band.wavelengths.value.min(), self.band.wavelengths.value.max(), 1) * u.nm
 
        # Interpolate throughput, filter transmission and QE to new grid
        tau = interp1d(self.optic.wavelengths, self.optic.throughput, kind='linear', fill_value='extrapolate')
        ft = interp1d(self.band.wavelengths, self.band.transmission, kind='linear', fill_value='extrapolate')
        qe = interp1d(self.camera.wavelengths, self.camera.QE, kind='linear', fill_value='extrapolate')

        # End-to-end efficiency. Need to put units back after interpolation
        effs = tau(waves) * ft(waves) * qe(waves) * u.electron / u.photon
        
        # Band averaged efficiency, effective wavelengths, bandwidth (STSci definition), flux_integral
        i0 = np.trapz(effs, x=waves)
        i1 = np.trapz(effs*waves, x=waves)
        self._iminus1 = np.trapz(effs/waves, x=waves ) # This one is useful later
        self._iminus2 = np.trapz(effs/waves**2, x=waves)
        
        self.wavelengths = waves
        self.efficiencies = effs
        self.efficiency = i0 / (waves[-1] - waves[0])
        self.mean_wave = i1 / i0
        self.pivot_wave = (i1 / self._iminus1)**0.5
        self.bandwidth = i0 / effs.max()    

    def _gamma0(self):
        """
        Calculates 'gamma0', the number of photons/second/pixel at the top of atmosphere 
        that corresponds to 0 AB mag/arcsec^2 for a given band, aperture & pixel scale.
        """
        # Spectral flux density corresponding to 0 ABmag, pseudo-SI units
        sfd_0 = (0 * u.ABmag).to(u.W / (u.m**2 * u.um), \
                                 equivalencies=u.equivalencies.spectral_density(self.pivot_wave))
        # Change to surface brightness (0 ABmag/arcsec^2)
        sfd_sb_0 = sfd_0 / u.arcsecond**2
        # Average photon energy
        energy = c.h * c.c / (self.pivot_wave * u.photon)
        # Divide by photon energy & multiply by aperture area, pixel area and bandwidth to get photons/s/pixel
        photon_flux = (sfd_sb_0 / energy) * self.optic.aperture_area * self.pixel_area * self.bandwidth
        
        self.gamma0 = photon_flux.to(u.photon/(u.s * u.pixel))
                     
                      