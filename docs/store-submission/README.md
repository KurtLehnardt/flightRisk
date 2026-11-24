# Store submission kit: owner checklist

Texts to paste are in this folder. Do the steps in order. Items marked OWNER need you personally.

Identifiers (from the code):
- iOS bundle ID: `com.flightrisk.app` (`ios/project.yml`, `PRODUCT_BUNDLE_IDENTIFIER`)
- Android package name: `com.flightrisk.app` (`mobile/app/build.gradle.kts`, `applicationId`)

## Checklist

1. [~] (in progress: the owner is installing it now) Install the released (non-beta) Xcode. A build from a beta Xcode cannot be submitted. Add the Metal Toolchain: `xcodebuild -downloadComponent MetalToolchain`.
2. [ ] The Play developer account is personal, so production needs a closed test first. Start the closed test as early as possible, because the 14 days cannot be shortened. Steps: (a) create the app in Play Console; (b) upload the AAB to a closed testing track; (c) add at least 12 testers (a Google Group or an email list) who stay opted in for 14 continuous days; (d) collect and note their feedback; (e) after 14 days, apply for production access, which asks about the test; (f) then submit to production review.
3. [x] Done: the privacy policy is hosted at https://github.com/KurtLehnardt/flightRisk/blob/main/docs/privacy-policy.md (the repo is public and the page loads). Set `FLIGHTRISK_PRIVACY_POLICY_URL` (iOS) and `-Pflightrisk.privacyPolicyUrl` (Android) to that address.
4. [ ] Create the app records: App Store Connect (bundle ID `com.flightrisk.app`, register it in your developer account first, put your team ID in `ios/Config/Signing.local.xcconfig`) and Play Console (package `com.flightrisk.app`).
5. [ ] Upload builds. iOS: archive, upload, higher build number each time, then TestFlight. Android: signed release app bundle (see `docs/android-release-signing.md`). Merge the model integration PRs first, or the Search tab shows "Person detection unavailable" and review will reject it.
6. [ ] Paste the texts: `app-store-listing.md` and `app-review-notes.md` into App Store Connect; `play-listing.md` into Play Console (listing, App access, Ads, content rating).
7. [ ] Complete App Privacy (Apple) and Data Safety (Google) from `privacy-answers.md` (written separately).
8. [ ] Screenshots (captured, see "Screenshots" below; the rest of this item still applies): iPhone 6.9-inch or 6.5-inch per App Store Connect's current list, no iPad set; Android phone screenshots, plus the feature graphic and icon for Play. Use demo mode so no real person appears.
9. [ ] Submit for review.

## OWNER must confirm personally

- The Play account is personal: the closed test with at least 12 testers for 14 continuous days applies.
- The age rating: iOS 17+ shows in App Store Connect; the IARC answers in `play-listing.md` are a draft.
- The App Privacy and Data Safety answers, including the "Linked" answer on iOS.
- Every `<OWNER: ...>` placeholder: support URL, marketing URL, contact email, phone, website.
- Review of the listing text by a lawyer, as the compliance doc advises, because the app handles images of people and children.
- The demo steps against the exact builds you upload. Button wording may differ.
- Whether the iPhone app is offered on Apple silicon Macs, and an on-device test on a real iPhone and Android phone. No physical device has run the iOS app.
- That the texts still match `docs/privacy-policy.md` once the model PRs merge.

## Wording rule

FlightRisk is a search aid, not a certified rescue system. Do not add words like "guaranteed", "certified" or "finds your child". Neither phone app does face detection or face recognition.

## Screenshots

Captured files are in `docs/store-submission/screenshots/ios/` and `docs/store-submission/screenshots/android/`.

- Leave out `01-search.png` on both platforms. It shows only a placeholder.
- The iOS ground and settings shots are cut off at the bottom by the tab bar.
- You may prefer new shots once the apps run on a real device with a drone.
- Demo timings differ by platform. iOS replays a scenario of about 37 s with its own timings; Android replays the 66 s scenario file. The review notes use each platform's own timings.
