# Bundled fonts

Both files are the **latin subset** of a Google Fonts variable font, fetched
once from `fonts.gstatic.com` and committed here. They are bundled rather than
linked because the device has no internet: a `<link>` to Google Fonts would
render the whole ambient UI in the system fallback face.

| File | Family | Axes | Used for |
|---|---|---|---|
| `space-grotesk-latin.woff2` | Space Grotesk | weight 300-700 | every numeral -- clock, city times, temperatures, scores, prices |
| `inter-latin.woff2` | Inter | weight 100-900 | body text and labels |

Both are licensed under the SIL Open Font License 1.1, which permits
redistribution as part of an application. Latin-only is deliberate: the full
family with every unicode range is several times the size, and nothing this UI
renders leaves Latin-1.

To refresh, re-fetch the `latin` `src` URL from
`https://fonts.googleapis.com/css2?family=Space+Grotesk` (or `Inter`) with a
desktop browser user-agent, which is what selects the woff2 subset.
