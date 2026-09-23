# Design system

## Surface

The product is an evidence-review workspace for contact-centre calls. The route-control board is the guiding system: a reviewer follows a call through a time-ordered record and verifies each finding against its source utterance.

## Visual grammar

- Use a cool, near-white working surface with charcoal text and one restrained amber selection color. Finding states also carry explicit text and distinct markers; never rely on color alone.
- Use fine horizontal and vertical rules as timeline tracks, table divisions and evidence connectors. Keep layout flat; elevation is reserved for the selected review rail only.
- Keep compact, aligned metadata and tabular numerals for call offsets, timestamps, scores and versions. Use system UI sans-serif for legibility and avoid display styling on operational data.
- Use consistent 6px control corners and visible high-contrast focus rings. Status labels remain ordinary readable text.
- Motion is limited to purposeful state feedback. Honor `prefers-reduced-motion`; queue selection and keyboard navigation remain immediate.
- Keep the audit and decision rail visible beside long transcripts. Below 860px, place it above the transcript and leave a clear scroll gap so focused evidence remains below the sticky rail.
- Show the validated citation quote on each audit evidence control and highlight that exact text in its canonical utterance. Render transcript and citation text as text, never markup.

## Product constraints

- This pilot supports manual WAV/MP3 uploads. No telephony provider is connected.
- Provider capability claims must distinguish live media, post-call recordings and call events. Unverified protocols and account entitlements remain visibly pending.
- The provider coverage page reports public documentation separately from project qualification; the MyOperator API documents recording links as valid for 24 hours.
- Use synthetic examples only. Keep transcript redaction, tenant authorization, human review, and permission-gated playback behavior from the server contracts.
