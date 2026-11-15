# Third-party software of the interface

Exact versions are pinned in `package.json` and `package-lock.json`; `npm ci` installs them in the build stage and in
`ui-dev`. Node itself is only in those stages (the image `node:24.20.0-bookworm-slim`), never in the image that runs.

What ends up in the bundle served to the browser:

| Package | Version | License | Use |
| --- | --- | --- | --- |
| `react`, `react-dom` | 19.3.1 | MIT | the interface (D7 of `UI-SPEC.md`); `scheduler` (MIT) comes with `react-dom` |
| `@fontsource/ibm-plex-sans` | 5.3.0 | OFL-1.1 | IBM Plex Sans, the text (woff2, latin) |
| `@fontsource/ibm-plex-mono` | 5.3.0 | OFL-1.1 | IBM Plex Mono, numbers and durations (woff2, latin) |
| `@fontsource/bricolage-grotesque` | 5.3.0 | OFL-1.1 | Bricolage Grotesque, the titles (woff2, latin) |

The fonts are distributed under the SIL Open Font License 1.1, whose text is in the `LICENSE` file of each
`@fontsource` package.

Only for the build and the tests (not in the image that runs):

| Package | Version | License |
| --- | --- | --- |
| `esbuild` | 0.28.2 | MIT |
| `typescript` | 7.0.2 | Apache-2.0 |
| `@types/node` | 24.19.2 | MIT |
| `@types/react`, `@types/react-dom` | 19.3.0 | MIT |
