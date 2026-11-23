# App Review notes (iOS)

Paste the text between the lines into the "Notes" field of App Store Connect > App Review Information. Source: `docs/app-store-compliance.md`, "App Review Notes". Check it against the build you upload.

Sign-in required: **No.** Leave the demo account fields empty.

Contact (App Review Information fields): first name, last name, phone `<OWNER: phone>`, email `<OWNER: contact email>`.

---

FlightRisk is a search aid for parents and guardians who are looking for a missing child. It is not a certified rescue system. Person detection and appearance matching run on the device with Core ML. The app does not do face detection or face recognition; it matches by appearance (clothing, colours, build). The app has no accounts, no login, no ads and no analytics.

REAL USE NEEDS HARDWARE YOU DO NOT HAVE. Full use needs a drone and a laptop "ground station" that the user runs on their own network. Please use the built-in demo mode, which needs no drone, no ground station, no photo, no account and no network.

HOW TO OPEN DEMO MODE AND WHAT YOU WILL SEE
1. Install and open FlightRisk.
2. The first screen is the onboarding privacy notice. Tap "Try the demo (no drones, no photo)" on the onboarding screen. (You can also complete onboarding and go to Settings > Demo > "Demo mode".)
3. A purple "DEMO – no real drones" banner stays above every tab until you tap "Exit demo".
4. Open the Ground tab. A simulated ground station with two simulated drones, demo-drone-1 and demo-drone-2, replays a scripted scenario of about 37 seconds. It shows "DEMO: start" at about 3 s, the drones taking off and flying their lanes from about 5 s, a "DEMO: OBSTACLE" notice at about 12 s when demo-drone-2 meets an obstacle and moves to its next lane, and a "possible person match" alert at about 14 s. The alert may be below the visible area, so scroll down the Ground tab to find it. At about 22 s the alert gets an AI verdict ("AI check: match (high confidence)", with details under "AI check details"). At about 30 s the simulated operator confirms the alert. At about 33 s the demo sends "DEMO: LAND ALL sent" by itself and the drones land, and the scenario ends at about 37 s.
5. You can tap "LAND ALL (demo)" or a drone's Land button at any time. The result starts with "DEMO:" and is answered on the phone.
6. The Search and Target tabs show a drawn placeholder figure in demo mode. They run no search. A live search needs a real drone and a camera, which the demo does not use. The camera, photo library and photo picker are not used in demo mode.
7. "Exit demo" clears everything the demo showed. "Play the demo again" in Settings restarts it.

LOCAL NETWORK PERMISSION. The Ground tab can connect to a ground station, a computer the user runs on their own network. The user types its address in Settings (for example ws://192.168.1.20:5555) and a key, which is stored in the Keychain. The app connects to that address and to no other. The permission prompt appears only when a real ground station address is used. In demo mode, or with the address left empty, the app makes no local network connection and the prompt does not appear.

APP TRANSPORT SECURITY. Info.plist sets NSAllowsLocalNetworking only, so that the plain-HTTP health check to a ground station on the local network works. There is no NSAllowsArbitraryLoads and no exception domain. The WebSocket link uses Network.framework, to which App Transport Security does not apply. The app itself refuses cleartext (ws:// and http://) connections unless the address resolves to a local network address, checked before the key or any request is sent. wss:// and https:// addresses are accepted. The first-launch notice and Settings tell the user that a ws:// link is not encrypted and to use a trusted network.

CLOUD REASONING IS OPT-IN AND OFF BY DEFAULT. Reviewers can leave it off; every other feature works without it. It sends the reference photo, the cropped image of a detected person and the typed description to Anthropic only after the user turns on "Allow cloud reasoning" and confirms the dialog, saves their own Claude API key and selects Cloud Claude. With the default settings no image of any person leaves the device.

OTHER NETWORK USE. Settings can download an on-device language model from Hugging Face when the user taps the download button. Nothing is downloaded otherwise.

PERMISSIONS. Camera: to scan for people matching the reference photo. Photo library: to choose the reference photo. Location when in use: to attach GPS coordinates to a match alert. Local network: described above. A privacy notice is shown before the camera starts.

Contact for questions: <OWNER: name, email, phone>.

---

Owner checks before pasting:
- Confirm the Info.plist strings and the demo steps against the exact build you upload. Button wording changes break these steps.
- The iOS demo steps come from `docs/ios-ground-client.md` ("Demo mode") and the compliance doc. Run them on a device or simulator once.
- Do not submit until the model integration PRs are merged. Without them the Search tab shows "Person detection unavailable" and review will reject the app.
