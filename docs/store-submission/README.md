# Store submission kit: owner checklist

Texts to paste are in this folder. Do the steps in order. Items marked OWNER need you personally.

Identifiers (from the code):
- iOS bundle ID: `com.flightrisk.app` (`ios/project.yml`, `PRODUCT_BUNDLE_IDENTIFIER`)
- Android package name: `com.flightrisk.app` (`mobile/app/build.gradle.kts`, `applicationId`)

## Checklist

1. [ ] Install the released (non-beta) Xcode. A build from a beta Xcode cannot be submitted. Add the Metal Toolchain: `xcodebuild -downloadComponent MetalToolchain`.
2. [ ] Check the Play Console account type: Play Console > Settings > Developer account > About you / Account type. A personal account created after November 2023 must run a closed test with at least 12 testers for 14 days before it can apply for production. An organization account does not. Start the closed test early if it applies.
3. [ ] Host the privacy policy at a public HTTPS web page (not a PDF or a source file). Use `docs/privacy-policy.md` after the model PRs merge. Set `FLIGHTRISK_PRIVACY_POLICY_URL` (iOS) and `-Pflightrisk.privacyPolicyUrl` (Android) to that address.
4. [ ] Create the app records: App Store Connect (bundle ID `com.flightrisk.app`, register it in your developer account first, put your team ID in `ios/Config/Signing.local.xcconfig`) and Play Console (package `com.flightrisk.app`).
5. [ ] Upload builds. iOS: archive, upload, higher build number each time, then TestFlight. Android: signed release app bundle (see `docs/android-release-signing.md`). Merge the model integration PRs first, or the Search tab shows "Person detection unavailable" and review will reject it.
6. [ ] Paste the texts: `app-store-listing.md` and `app-review-notes.md` into App Store Connect; `play-listing.md` into Play Console (listing, App access, Ads, content rating).
7. [ ] Complete App Privacy (Apple) and Data Safety (Google) from `privacy-answers.md` (written separately).
8. [ ] Screenshots (to be generated): iPhone 6.9-inch or 6.5-inch per App Store Connect's current list, no iPad set; Android phone screenshots, plus the feature graphic and icon for Play. Use demo mode so no real person appears.
9. [ ] Submit for review.

## OWNER must confirm personally

- Account type and the 12-tester, 14-day rule for Play.
- The age rating: iOS 17+ shows in App Store Connect; the IARC answers in `play-listing.md` are a draft.
- The App Privacy and Data Safety answers, including the "Linked" answer on iOS.
- Every `<OWNER: ...>` placeholder: support URL, marketing URL, contact email, phone, website.
- Review of the listing text by a lawyer, as the compliance doc advises, because the app handles images of people and children.
- The demo steps against the exact builds you upload. Button wording may differ.
- Whether the iPhone app is offered on Apple silicon Macs, and an on-device test on a real iPhone and Android phone. No physical device has run the iOS app.
- That the texts still match `docs/privacy-policy.md` once the model PRs merge.

## Wording rule

FlightRisk is a search aid, not a certified rescue system. Do not add words like "guaranteed", "certified" or "finds your child". Neither phone app does face detection or face recognition.
