"""Flight parameter registry types and operator message texts.

This package must stay importable without the web, vision or drone driver
libraries (flask, ultralytics, torch, cv2, mavsdk, djitellopy). It changes no
runtime behaviour: nothing outside it imports it yet.
"""
