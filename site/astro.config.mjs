import { defineConfig } from "astro/config";

export default defineConfig({
  output: "static",
  base: process.env.ASTRO_BASE_PATH || "/",
});
