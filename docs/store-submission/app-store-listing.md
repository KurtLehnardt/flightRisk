# App Store listing text (iOS)

Draft for the owner to paste into App Store Connect. Every limited field shows its character count. Counts are Python `len()` of the text inside the code block.

Bundle ID: `com.flightrisk.app` (from `ios/project.yml`).

## Version information

**App name** (10/30 characters)

```
FlightRisk
```

**Subtitle** (30/30 characters)

```
Search aid for a missing child
```

**Promotional text** (154/170 characters)

```
Helps you look for a missing child. Match a reference photo against your phone camera or a drone feed, on your device. A search aid, not a rescue service.
```

**Description** (2722/4000 characters)

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
- The phone does not connect to a drone directly. A real drone search needs a drone and a laptop ground station.
- The link to your ground station is on your local network and is not encrypted unless you use a wss:// or https:// address. Use a network you trust.

TRY IT WITHOUT A DRONE
- Tap "Try the demo (no drones, no photo)" on the first screen. A short simulated search with two simulated drones shows how the Ground tab works. The demo uses no camera, no photo and no network.

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

**Keywords (comma-separated, no spaces after commas)** (95/100 characters)

```
missing child,search,find person,drone,lost child,rescue aid,camera,reunite,search party,locate
```

**Support URL:** `https://<OWNER: public support page URL>` (required)

**Marketing URL:** `https://<OWNER: optional product page URL>`

**Privacy Policy URL:** `https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md` (required; the same address as the `FLIGHTRISK_PRIVACY_POLICY_URL` build setting)

**Copyright:** `© 2026 Kurt Lehnardt`

## Category

- Primary: **Utilities** (recommended).
- Secondary: Navigation (optional).
- Why: the app is not a map or turn-by-turn app. It matches people on a camera view and operates a ground console. Navigation could mislead users into expecting maps and routes, and invites a review for map features the app does not have. The owner may choose Navigation if they plan to add the map work tabled in the roadmap.

## Age rating

Result to expect: **17+** (the compliance doc, "Age Rating"). Why, in plain words:

- App Store Connect's questionnaire has no question about missing-child or crisis use. The app has none of the listed content (violence, sexual content, gambling, horror, profanity, drugs, contests).
- The rating is chosen deliberately: the subject is a safety and crisis context involving missing children, and the app sees and matches images of real people. It is meant for adult users only.
- Answer the questionnaire truthfully. If it returns a lower rating than 17+, check whether App Store Connect lets you set a higher rating, and decide with the owner.
- OWNER MUST CONFIRM: that App Store Connect actually shows 17+ and that the owner accepts the wording above.

## Notes

- The description says what the app does and does not do. Do not add claims such as "finds your child", "guaranteed", "certified" or "rescue system".
- The description names Anthropic only for the opt-in cloud feature, as the privacy policy does.
- Keep the description in line with `docs/privacy-policy.md` after the model PRs merge.
