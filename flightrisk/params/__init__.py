"""Flight parameter registry types and operator message texts.

This package must stay importable without the web, vision or drone driver
libraries (flask, ultralytics, torch, cv2, mavsdk, djitellopy). A module
outside params and hardware may import it at run time only when
tests/param_adopters.py names it; flightrisk/drone/mavlink.py does, for the
legacy controller's own limits in legacy.py.
"""
