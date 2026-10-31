# FlightRisk Mobile -- First-Launch Privacy Notice Spec

---

## Purpose

A mandatory privacy notice screen shown to the user on first launch, **before** the camera permission prompt is triggered. The user must acknowledge the notice before proceeding.

---

## Trigger Conditions

- Shown on the very first app launch (or after app data is cleared).
- Intended to be shown **before** any camera or location permission dialog. On iOS this holds. **On Android it does not:** `MainActivity.onCreate` calls `requestPermissionsIfNeeded()` (camera, fine and coarse location) before it sets the content, so the system permission prompts appear on first launch before the notice. This is listed in `docs/mobile-followups.md`.
- Not shown again after the user has acknowledged it (controlled by a persisted flag). On Android the flag is written but never read; see "Persistence" below.

---

## Screen Layout

### Title

**"Privacy Notice"** on both platforms.

### Body Text

The text below is what the iOS app shows. The wording of the ground station and cloud items matches the Ground section and the cloud consent dialog in Settings (`SettingsView.cloudConsentMessage`), and a unit test checks the key phrases.

> **FlightRisk uses your phone's camera to help you locate a missing child.**
>
> - **Camera Access.** FlightRisk uses your camera to scan for people who match the target photo you provide.
> - **On-Device Processing.** Person detection and matching run on your device. Camera frames are not uploaded or stored.
> - **Ground Station (Optional).** If you add a ground station address in Settings, the app connects to that address only, to show drone status and send land, resume-search and detection-profile commands. The app refuses a ws:// or http:// address unless it is on your local network, and it checks that before anything, including the key, is sent. A ws:// or http:// link is not encrypted, so use a network you trust. Away from your local network, use a wss:// or https:// address. A VPN also works if it gives the ground station an address in a private range; addresses in 100.64.0.0/10, which Tailscale uses, are refused for ws:// and http://. The app does not pin certificates. The station's key is sent to the address you enter and is kept on this phone in the iOS Keychain.
> - **Cloud Reasoning (Off by Default).** Cloud reasoning is off. If you turn it on in Settings, save a Claude API key and select Cloud Claude, the cropped image of each detected person, the reference photo and the text description you entered are sent to Anthropic. The images can show a face. You can turn it off at any time.
> - **Location Data.** GPS coordinates are attached to match alerts so you can navigate to the location. Location data stays on your device.
> - **No Analytics or Accounts.** FlightRisk does not collect analytics or telemetry. There is no account or sign-in.

### Android body text

This is the text in `mobile/app/src/main/java/com/flightrisk/app/ui/onboarding/OnboardingScreen.kt` (`PrivacyNoticeStep`). The cloud item uses the same words as the iOS cloud item. The Android notice has **no ground station item** and **no privacy policy link**: the ground station link, the phone-as-edge mode and the direct Tello link have their own screens and, for phone-as-edge, a disclosure and consent step in Settings, but the first-launch notice does not mention them. This is listed for the owner to decide (`docs/mobile-followups.md`).

> - **Camera Access.** FlightRisk uses your camera to scan for people who match the target photo you provide.
> - **On-Device Processing.** Person detection and matching run on your device. Camera frames are not uploaded. Images leave the phone only with cloud reasoning on, as described below.
> - **Cloud Reasoning (Off by Default).** Cloud reasoning is off. If you turn it on in Settings, save a Claude API key and select Cloud Claude, the cropped image of each detected person, the reference photo and the text description you entered are sent to Anthropic. The images can show a face. You can turn it off at any time.
> - **Location Data.** GPS coordinates are attached to match alerts so you can navigate to the location. Location data stays on your device.
> - **No Data Collection.** FlightRisk does not collect analytics, telemetry, or personal data. There is no account or sign-in.

### Privacy Policy Link

A tappable link below the bullets:

**"Read the full privacy policy"**

- Opens the system browser.
- URL on iOS: the public repository copy of `docs/privacy-policy.md` (`https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md`). This is a stand-in. When a hosted page exists, change `PrivacyNotice.policyURL` and use the same URL in App Store Connect.

### Acknowledgment Button

A single primary action button at the bottom of the screen:

**"I Understand"**

- Tapping this dismisses the notice and proceeds to the next onboarding step. On iOS the camera is started only after onboarding finishes, so the camera permission prompt comes after the notice.
- The acknowledgment is recorded (see Persistence below).
- The button must be clearly visible and not obscured by the body text on any screen size.

---

## Persistence

### iOS

- Store acknowledgment as a boolean in `UserDefaults`:
  - Key: `flightrisk_privacy_notice_acknowledged`
  - Value: `true` when the user taps "I Understand"
- The earlier iOS builds had no separate key. They recorded only `flightrisk_onboarding_complete`, which is written when the whole onboarding flow finishes ("Start Searching"). That key is still written at the end of onboarding and still decides whether the onboarding flow is shown at all.
- The app treats the notice as acknowledged when either key is true.
- Decision for people who finished onboarding under the old notice: they are not asked again. The notice changed in two ways and neither widens what an existing install does. The ground station link is off until the person types an address in Settings, and iOS is expected to show its own local network permission prompt when the app first connects to a local address (not verified on a device). The cloud wording became more complete and stricter: cloud reasoning now needs the opt-in toggle on top of a key, so it can only send less than before. If a later change would make an existing install send more data, the notice must be shown again, which needs a new key name or a version number.
- `PrivacyNotice.migrateLegacyAcknowledgement` copies an old acknowledgement to the new key and never clears either key. It is idempotent and runs the first time the notice state is read. That read happens only on the onboarding code path, so a user who finished onboarding and never sees it again keeps only the old key; reading either key gives the right answer.
- A person who tapped "I Understand" but quit before finishing onboarding is not shown the notice a second time; onboarding resumes at the next step.
- Deviation from the update plan (R-24): the plan says the notice sets both keys. Setting `flightrisk_onboarding_complete` at the notice would end onboarding before the photo step, so the notice sets only the new key and the old key keeps its meaning.

### Android

- The acknowledgment is written as a boolean to the `SharedPreferences` file `flightrisk_prefs`:
  - Key: `flightrisk_privacy_notice_acknowledged`
  - Value: `true` when the user taps "I Understand"
- **Nothing reads that key.** At launch the app reads only `flightrisk_onboarding_complete` (same file), which is written when the user accepts the photo at the end of onboarding. Onboarding always starts at the notice, so a person who tapped "I Understand" but quit before finishing is shown the notice again. This is different from iOS, where the notice step is skipped in that case.

### Check Logic (iOS)

```
on app launch:
  if flightrisk_onboarding_complete:
    show main app            # old acknowledgement counts
  else if flightrisk_privacy_notice_acknowledged:
    show onboarding after the notice
  else:
    show PrivacyNoticeScreen
    on "I Understand" tapped:
      set flightrisk_privacy_notice_acknowledged = true
      continue onboarding
```

On Android the check is simply: if `flightrisk_onboarding_complete` is true, show the main app; otherwise show onboarding from the notice.

---

## Design Guidelines

- **Background:** Solid, matching the app's primary background color. No busy imagery.
- **Text:** High contrast, readable font size (minimum 16sp / 16pt body text).
- **Scrollable:** The body text area should be scrollable if it exceeds the visible area on smaller screens.
- **Button:** Full-width or near-full-width primary button, visually prominent, at the bottom of the screen. Fixed position (not scrolled away).
- **No dismiss gesture:** The user cannot swipe away, tap outside, or use the back button to bypass the notice. The only way past is tapping "I Understand."
- **Accessibility:** All text must be compatible with VoiceOver (iOS) and TalkBack (Android). The button must have a clear accessibility label.

---

## Testing Checklist

- [ ] Notice appears on first launch
- [ ] Notice does not appear on subsequent launches after acknowledgment
- [ ] Notice reappears if app data/storage is cleared
- [ ] "I Understand" button records the flag correctly
- [ ] Camera permission prompt appears only after acknowledgment (iOS; fails on Android today, see above)
- [ ] Privacy policy link opens correctly
- [ ] A device with only the old key set goes straight to the main app and is not asked again
- [ ] A device with only the new key set resumes onboarding after the notice
- [ ] Screen is scrollable on small devices
- [ ] Screen is accessible via VoiceOver / TalkBack
- [ ] Back button / swipe-to-dismiss does not bypass the notice
