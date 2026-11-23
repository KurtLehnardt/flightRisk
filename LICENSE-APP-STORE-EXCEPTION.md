# FlightRisk App Store Additional Permission

> **DRAFT FOR THE OWNER'S REVIEW. NOT IN FORCE.**
> This is a draft prepared by an AI assistant. It is not legal advice. The copyright holder should have it reviewed by a lawyer experienced in open-source licensing before adopting it. It takes effect only when the copyright holder adopts it, for example by committing it and referring to it from the README and source headers.

## Preamble (why this exists)

FlightRisk is licensed under the GNU General Public License, version 3 (GPLv3), the text of which is in the `LICENSE` file. Mobile app stores, such as Apple's App Store and Google Play, require users to accept store terms. Those terms can include usage rules, DRM, and limits on how many devices may run an app. GPLv3 forbids adding further restrictions of that kind when object code is conveyed. Many people read this as a conflict between GPLv3 and some stores' terms.

This is a common problem for copyleft projects, and projects have handled it in different ways. For example, VLC's iOS app has been the subject of public discussion and relicensing efforts aimed at the same conflict. This document does not state or rely on any specific legal fact about those projects. It simply takes the approach the GPL itself provides: an additional permission under GPLv3 section 7, granted by the copyright holder.

## 1. Additional permission

Under GPLv3 section 7, the copyright holder of FlightRisk grants you this additional permission.

You may convey the covered work, in object code form, through an app store or similar distribution service, even if that service's terms of service impose restrictions that GPLv3 would otherwise forbid. Examples of such restrictions include usage rules, digital rights management, and limits on the number of devices. "The covered work" here means FlightRisk's iOS and Android apps and the code in this repository that the copyright holder owns.

Apple's App Store and Google Play are examples. The permission is not limited to them.

## 2. Source code stays available

This permission does not remove the requirement to make source code available. The Corresponding Source of the covered work remains available under GPLv3 at the public repository:

`<repository URL>` (proposed value: `https://github.com/KurtLehnardt/flightRisk`)

Anyone who conveys the work under this permission must still comply with GPLv3 in all other respects, including providing or offering the Corresponding Source and keeping the licence notices.

## 3. Scope: only code the copyright holder owns

This permission applies only to code whose copyright is held by the copyright holder (Kurt Lehnardt). It does not change the licence of any third-party component. Third-party components, including software libraries and machine-learning models, keep their own licences and notices, and anyone conveying the work must comply with them. Nothing in this document grants rights in third-party material.

## 4. Removal by modifiers

As provided in GPLv3 section 7, if you modify the covered work, you may remove this additional permission from your version of the work, or from any part of it. If you do not remove it, it continues to apply to your version, but only to the code the copyright holder owns.

## 5. Third-party notices the apps must show in their licence screens

Each app's licence screen must carry at least these notices.

| Component | Licence | Note |
|---|---|---|
| mlx-swift | MIT | |
| mlx-swift-examples | MIT | |
| swift-transformers | Apache-2.0 | |
| swift-jinja | Apache-2.0 | |
| swift-collections | Apache-2.0 | |
| swift-numerics | Apache-2.0 | |
| GzipSwift | MIT | |
| YOLOX-S model | Apache-2.0 | Include its NOTICE file |
| CLIP ViT-B-16 (laion2b) model | MIT | |
| Pillow `Resample.c` (basis of the iOS CLIP resize port) | MIT-CMU (HPND) | Include the Pillow copyright and permission notice |
| OpenCV | Apache-2.0 | Optional credit |

Implementation note: the iOS app's licence screen shows `model-notices.md`, which comes from the model notices file on the model PR branches. The model entries above (YOLOX-S, CLIP, and the Pillow notice) must be added there once those PRs merge. Until then the iOS licence screen is incomplete.

## 6. Notes for the owner (delete before adopting)

- An audit found all commits are by the owner (olsonjb is a former identity of the owner) or AI-assisted commits owned by him, with no outside contributors. That is the factual basis for the owner being able to grant this permission alone. Any future outside contribution would need the contributor's agreement (for example a CLA) before this permission could cover it.
- Decisions: which stores to name; whether to add the notice to source file headers and the README; whether to cover Android and iOS only or any future app.
