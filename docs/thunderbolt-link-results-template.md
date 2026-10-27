# Thunderbolt link results

Copy this file, fill it in while you run the validation checklist in `docs/fleet-setup.md` (Path 3), and keep the copy with the field-test notes. Replace each blank with the value you measured. Do not write secrets or tokens here.

```
Date/time:            Operator:
Mac model / macOS:            Windows model / version (winver):
Win TB/USB4 controller (Device Manager):          Cable model:
Step 3 link up?  Mac bridge0 status:      Win adapter name/description/speed:
Step 4 IPs set?  Mac 10.42.0.1  Win 10.42.0.2   MTU Win:      route get OK?
Step 5 ping loss/avg/max (Mac->Win):          (Win->Mac):
       check_fleet_link PASS/FAIL, median RTT:        iperf3 (optional):
       soak (Win->Mac, --soak 120) loss %:   rtt p50/p95/max:   max gap:
Step 6 unplug: ping fail after __ s; recover after replug __ s; Win IP kept? Y/N; Mac IP kept while unplugged? Y/N
       sleep/power settings applied? Mac Y/N  Win Y/N
Step 7 Tello hw_check both laptops PASS/FAIL; video frames Mac __ Win __; cable loss during video __%
Step 8 fake fleet over cable: edge-2 land at __ s; ground lost at __ s; re-task OK? Y/N;
       Mac edge-1 survived cable pull? Y/N;  edge-2 rejoin after replug __ s
Decision: rung 1 / 2 / 3 / 4
```
