# FlightRisk Mobile -- First-Launch Privacy Notice Spec

---

## Purpose

A mandatory privacy notice screen shown to the user on first launch, **before** the camera permission prompt is triggered. The user must acknowledge the notice before proceeding.

---

## Trigger Conditions

- Shown on the very first app launch (or after app data is cleared).
- Shown **before** any camera or location permission dialog is presented.
- Not shown again after the user has acknowledged it (controlled by a persisted flag).

---

## Screen Layout

### Title

**"Before We Start"**

### Body Text

> **FlightRisk uses your phone's camera to help you locate a missing child.**
>
> Here's how it works:
>
> - The camera scans for people and compares their faces and appearance against the reference photo you provide.
> - **All detection and matching happens entirely on your device.** No biometric data (face scans, body features) ever leaves your phone.
> - Match thumbnails and locations are stored locally on your device and can be deleted at any time.
> - An optional cloud AI feature can provide additional analysis. If you enable it, only cropped person images are sent to the AI provider you choose. You will be clearly warned before this is activated.
>
> No accounts. No tracking. No data shared with third parties.

### Privacy Policy Link

A tappable text link below the body:

**"Read our full Privacy Policy"**

- Opens the privacy policy in an in-app browser or the system browser.
- URL: The hosted version of `docs/privacy-policy.md`.

### Acknowledgment Button

A single primary action button at the bottom of the screen:

**"I Understand"**

- Tapping this dismisses the notice and proceeds to the camera permission prompt.
- The acknowledgment is recorded (see Persistence below).
- The button must be clearly visible and not obscured by the body text on any screen size.

---

## Persistence

### iOS

- Store acknowledgment as a boolean in `UserDefaults`:
  - Key: `flightrisk_privacy_notice_acknowledged`
  - Value: `true` when the user taps "I Understand"

### Android

- Store acknowledgment as a boolean in `SharedPreferences`:
  - Key: `flightrisk_privacy_notice_acknowledged`
  - Value: `true` when the user taps "I Understand"

### Check Logic

```
on app launch:
  if NOT privacyNoticeAcknowledged:
    show PrivacyNoticeScreen
    on "I Understand" tapped:
      set privacyNoticeAcknowledged = true
      proceed to camera permission request
  else:
    proceed to main app
```

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
- [ ] Camera permission prompt appears only after acknowledgment
- [ ] Privacy policy link opens correctly
- [ ] Screen is scrollable on small devices
- [ ] Screen is accessible via VoiceOver / TalkBack
- [ ] Back button / swipe-to-dismiss does not bypass the notice
