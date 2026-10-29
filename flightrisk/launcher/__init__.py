"""FlightRisk launcher: pick a fleet setup in a window and start the ground station and edges.

The package is split so that everything except the window is plain Python
with no user-interface imports, and is unit-tested on its own:

* ``settings``: the launcher settings, built-in and saved presets, validation.
* ``commands``: the command lines and environments of the child processes,
  and the edge token (read from the environment or ``~/.flightrisk-fleet.env``,
  passed to children through their environment only, and redacted everywhere).
* ``netinfo``: this computer's network adapters and their IPv4 addresses.
* ``preflight``: read-only checks before a start, including a Tello
  reachability probe that sends only ``command``, ``battery?`` and ``temp?``.
* ``processes``: starts, watches, restarts and stops the child processes the
  launcher started, and only those.
* ``server``: the window, a small web page served on 127.0.0.1.

The launcher never sends a flight command. See docs/launcher.md.
"""
