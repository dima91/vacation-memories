// The browser tests of the editor (UI-SPEC.md, D14). They run in the `e2e` container, which shares the network of nginx:
// the page is `http://localhost:8080`, the address the editor accepts for `Host` and `Origin`.
import { defineConfig } from "@playwright/test";

export const BASE_URL = "http://localhost:8080";

export default defineConfig({
  testDir: "tests",
  globalSetup: "./global-setup.ts",
  outputDir: "results",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  workers: 1, // one project, one plan: the tests take turns
  fullyParallel: false,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    viewport: { width: 1440, height: 900 }, // the reference window of section 10
    locale: "it-IT",
    timezoneId: "Europe/Rome",
    extraHTTPHeaders: { Origin: BASE_URL },
    screenshot: "only-on-failure",
    trace: "off",
    launchOptions: { args: ["--no-sandbox"] }, // a container without the host's user namespaces
  },
});
