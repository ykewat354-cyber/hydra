---
name: website-3d
description: Build or upgrade a website/3D page — reuse the existing project, real Three.js geometry, host and verify.
roles: lead, coder, worker, planner
---
# Website / 3D recipe

## Before writing anything
- Look at the Workspace section in your env block and run `ls`/`glob`.
  If the task names an existing site, or a site directory already exists
  (`gym_website/`, `site/`, a folder with `index.html`), **modify that
  project in place**. Never create `site2/`, `website-final/`, `3d_website/`
  next to it.
- Read the existing `index.html`, CSS and JS before editing; keep the real
  content (headings, sections, contact details) that is already there.
- If both `assets/css/` and `css/` (or `assets/js/` and `js/`) exist, one
  tree is stale. Keep the one the HTML actually references, merge anything
  useful, and delete the other.

## Definition of Done
- One canonical project directory. No duplicate `css/`, `js/`, `assets/`,
  `images/` trees at the same level.
- Every local `href`/`src` resolves to a file that exists.
- `<!doctype html>`, `<html>`, `<head>`, `<body>` present; real sections with
  content, not "Lorem ipsum".
- If the task asks for 3D: use Three.js (or raw WebGL) with real geometry
  and materials — extruded shapes, `TorusKnotGeometry`, `SphereGeometry`,
  `InstancedMesh`, sprites or loaded models. A sphere body plus two Sprite
  wings is a placeholder, not a butterfly: build each wing as a
  `BufferGeometry`/`ShapeGeometry` mesh that rotates around the body, and
  render 8–20 butterflies. Clouds of flat `PlaneGeometry` quads are not a
  butterfly either.
- Roman Hinglish "fir"/"phir" means "then" — never put a literal "FIR" text
  sprite or heading in the scene or page.
- Add `AmbientLight` + directional/point lights, a camera, an
  `requestAnimationFrame` loop, and a `resize` handler.
- Scroll interaction must actually move things: read `window.scrollY` and
  lerp camera/object positions each frame; don't just set `camera.position.z`
  once.
- Build/serve step: run `npm install`, `npm run build` (if a bundler), then
  start the server with the `bash` tool and its `background=true` argument,
  e.g. `{"cmd": "cd gym_website && python3 -m http.server 8080 --bind 0.0.0.0",
  "background": true}`. Do not put `&` in the command string. Reuse one port;
  check `job_status`/`job_output` instead of starting a second server.
- Verify with `curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:PORT/`
  and fix until it returns 200. Report the exact URL.
- Before `finish`: mark every todo `x ...`; list every file you wrote or
  edited in `files_changed`; say how to stop a server you leave running.
  Use `write_file`/`edit_file`, not shell heredocs or `sed -i`, so the
  verifier can open the exact files you changed.
- Read each file once, write once, verify once with curl, then `finish`.
  Do not keep re-reading the same CSS/HTML while the turn cap is near.

## Reliable patterns
- Plain static site: `python -m http.server 8080 --bind 0.0.0.0 &` or
  `hydra` bash background; keep files relative (`./css/style.css`), so it
  also opens with `file://`.
- Vite/Three.js: `index.html` at project root + `main.js` as a module. Do not
  point the root HTML at `/assets/...` — that is the built hash file. Either
  reference `./main.js` for dev, or serve `dist/` and reference the hashed
  asset. Do not leave two versions with different entry points.
- `node --check <file>.js` for syntax; `npm run build` must exit 0.
- Keep `node_modules/` out of `files_changed`.
