# Google Play listing text (Android)

Draft for the owner to paste into Play Console. Counts are Python `len()` of the text in each code block.

Package name: `com.flightrisk.app` (from `mobile/app/build.gradle.kts`).

## Main store listing

**App name** (10/30 characters)

```
FlightRisk
```

**Short description** (70/80 characters)

```
A search aid for finding a missing child. Matching runs on your phone.
```

**Full description** (2739/4000 characters)

```
FlightRisk is a search aid for parents and guardians who are looking for a missing child. It is not a certified rescue system and does not replace calling the police or emergency services. If a child is missing, call your local emergency number first.

WHAT IT DOES
- You give the app a reference photo of the person you are looking for, and an optional short description.
- The app looks at the phone camera view and compares each person it sees with the reference photo. It reports possible matches with a confidence score.
- A possible match is a lead for you to check yourself. It can be wrong. Always confirm in person.

HOW MATCHING WORKS
- Person detection and matching run on your phone. The app matches people by appearance (clothing, colours, build). It does not do face detection or face recognition.
- Appearance data is kept in memory for the search only and is never sent anywhere.
- No account or login is needed. The app has no ads and no analytics.

GROUND CONSOLE FOR A DRONE SEARCH (OPTIONAL)
- FlightRisk can also work as an operator console for a FlightRisk ground station, a computer you run yourself on your own network.
- In Settings you type the address of your ground station. The Ground tab then shows the status of your drones and the search plan, shows possible match alerts, and lets you send commands such as Land all.
- The phone can also connect to a Tello drone on its own Wi-Fi from the Search screen. A full multi-drone search needs a drone and a laptop ground station.
- The link to your ground station is on your local network and is not encrypted unless you use a wss:// or https:// address. Use a network you trust.

TRY IT WITHOUT A DRONE
- Tap "Try demo" on the first screen. A simulated mission of about a minute, with two simulated drones, shows how the Ground tab works. The demo uses no camera, no photo and no network.

CLOUD REASONING (OPTIONAL, OFF BY DEFAULT)
- If you turn it on, add your own Claude API key and choose Cloud Claude, the app sends the reference photo, the cropped image of each possible match and your description to Anthropic for a second opinion. These images can show faces.
- With the default settings, no image of any person leaves your phone.

PRIVACY
- Location is read when a match is found, so you can navigate back to that spot. It is kept in memory only.
- Read the full privacy policy through the link in the app and on this page.

IMPORTANT LIMITS
- FlightRisk is an aid. It can miss a person and it can match the wrong person. Results depend on lighting, distance, clothing and camera quality.
- It is intended for adults. Do not rely on it as your only means of search, and do not use it to follow or identify people for any purpose other than looking for a missing child.
```

**Category:** Application type "App", category **Tools** (recommended). Alternatives: Maps and Navigation is not recommended (no maps or routes); Lifestyle is possible. Tags: choose generic ones that match, such as "Utilities".

**Contact email:** `<OWNER: public contact email>` (required, shown on the store page)

**Website:** `https://<OWNER: public website URL>` (optional)

**Phone:** `<OWNER: optional>`

**Privacy policy URL:** `https://<OWNER: hosted copy of docs/privacy-policy.md>` (required; must be a public web page, not a PDF or source file; the same address as the `flightrisk.privacyPolicyUrl` Gradle property)

## App access

Answer: **All functionality is available without special access**, through demo mode. No login, account or credentials exist. If the console form requires instructions, paste:

```
FlightRisk has no accounts or login. Real use needs a drone and a laptop ground station, so reviewers should use demo mode, which needs no drone, no ground station, no photo and no network.
1. Open the app and tap "I Understand" on the privacy notice.
2. Onboarding step 1 is the privacy notice. Tap "I Understand". Then answer the camera and location permission prompts; denying both is fine. Step 2 is the "Important / Call 911" screen. Tap "Try demo" there. (Or: Settings > Demo mode.)
3. An amber "DEMO – no real drones" banner shows at the top of every screen.
4. Open the Ground tab. Two simulated drones, demo-drone-1 and demo-drone-2, replay a scripted scenario of about 66 seconds. Fleet status and the search plan appear in the first seconds, and the drones start their lanes (shown as lane 1/3 and so on) at about 4 s. An obstacle cuts a lane short at about 14 s. A possible person match alert appears at about 26 s and may be below the visible area, so scroll down the Ground tab to find it. At about 32 s an AI check rates it a strong match; tap the alert to see the AI check details. The simulated operator confirms it at about 42 s.
5. From about 34 s to 54 s demo-drone-2 hovers with the status HELD and a "Resume search" button. Tap it to try it. At about 54 s the scenario lands the drones. You can also tap "LAND ALL (demo)" or a drone's Land button at any time. These are answered on the phone. Nothing is sent anywhere; every result starts with "DEMO:".
6. The Search tab shows "Connect to Drone" and runs no demo search. The Target tab shows a drawn placeholder instead of a photo. A live search needs a real drone and a camera.
7. Turn demo mode off in Settings to clear everything the demo showed.
Cloud reasoning is off by default and not needed.
```

## Ads

**Does your app contain ads?** No. The app has no ad SDK and shows no ads.

## Content rating (IARC questionnaire) DRAFT

OWNER MUST CONFIRM every answer in the live questionnaire. Question wording changes; answer what is true for the shipped build. Source: `docs/app-store-compliance.md`, "Age Rating (IARC)".

| Topic | Draft answer | Reason |
|---|---|---|
| Category | Utility, Productivity, Communication or Other (not Game, not Social) | Tool for a search; no social feed |
| Violence | No | None in the app |
| Sexuality, nudity | No | None |
| Profanity or crude humour | No | None |
| Controlled substances | No | None |
| Gambling or simulated gambling | No | None |
| Fear, horror | No | None |
| User-generated content or user interaction | No | Users do not share content with other users. The optional ground station link goes to the user's own computer |
| Shares user location with other users | No | Location stays in memory and is not sent to other users |
| Digital purchases | No | No purchases |
| Unrestricted internet access or web browsing | No | No browser. Network use is the user's ground station address, optional Anthropic cloud reasoning and an optional model download |
| Sensitive subject matter | Mention if the form asks | Missing-child search and crisis use. Expected result is a high age rating (about PEGI 18, USK 18, ESRB Mature 17+ or the regional equivalent), as the compliance doc states |

The compliance doc expects 17+ or the equivalent. If the questionnaire returns a lower rating, the owner decides whether to accept it. Do not answer falsely to raise or lower the rating.

## Other declarations (reminders, not drafted here)

- Data safety: see `privacy-answers.md`.
- Target audience: adults only (18+). Do not select children's age groups; the app is not for children. This keeps it out of Designed for Families.
- Government app: No. News app: No. Health app: No.
- Face-related claims: neither phone app does face detection or recognition. Say so consistently.
- Permissions: camera, fine and coarse location, internet, network and Wi-Fi state, vibrate. Rationale text: `docs/app-store-compliance.md`, "Permission Rationale".
