// Makes every program icon (exe, installer, taskbar) and the startup-screen logo from
// backend/static/logo.png. Runs by itself before `npm run dev` and `npm run build`,
// and in the GitHub release build, so there is nothing to run by hand.
import { spawnSync } from "node:child_process";
import { copyFileSync, existsSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const logo = join(root, "backend", "static", "logo.png");

if (!existsSync(logo)) {
  console.log("sync-logo: no backend/static/logo.png, keeping the icons already in src-tauri/icons.");
  process.exit(0);
}

const run = spawnSync("npx", ["tauri", "icon", logo], { cwd: root, stdio: "inherit", shell: true });
if (run.status !== 0) {
  console.error("sync-logo: could not build the icons from backend/static/logo.png.");
  console.error("Use a square PNG, 512 px or larger, with a transparent or solid background.");
  process.exit(1);
}

const ico = join(root, "src-tauri", "icons", "icon.ico");
if (!existsSync(ico) || statSync(ico).size < 1000) {
  console.error("sync-logo: icon.ico was not created properly.");
  process.exit(1);
}

// The startup screen and the app's sidebar show the logo small, so the 256 px version is plenty.
const small = join(root, "src-tauri", "icons", "128x128@2x.png");
copyFileSync(small, join(root, "splash", "logo.png"));
copyFileSync(small, join(root, "backend", "static", "logo-small.png"));
console.log("sync-logo: icons and startup logo updated from backend/static/logo.png");
