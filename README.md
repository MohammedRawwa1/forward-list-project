# React + Vite

This template provides a minimal setup to get React working in Vite with HMR and some ESLint rules.

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Babel](https://babeljs.io/) (or [oxc](https://oxc.rs) when used in [rolldown-vite](https://vite.dev/guide/rolldown)) for Fast Refresh
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/) for Fast Refresh

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend using TypeScript with type-aware lint rules enabled. Check out the [TS template](https://github.com/vitejs/vite/tree/main/packages/create-vite/template-react-ts) for information on how to integrate TypeScript and [`typescript-eslint`](https://typescript-eslint.io) in your project.

# DafsaGame — DAFSA-converted Website & Phaser Frame Game

Overview
--------

This repository is a small web application that demonstrates a workflow around DAFSA (Deterministic Acyclic Finite State Automaton) schemas and an integrated Phaser game. Key pieces:

- A Python Flask backend that builds, serves, and mutates a DAFSA in-memory (`backend/app.py`).
- A Vite + React frontend that includes a Phaser game which uses a GIF→spritesheet asset for the player animation (`frontend/`).
- A Python utility that converts an animated GIF into a single spritesheet + JSON atlas usable by Phaser (`backend/convert_gif_to_spritesheet.py`).

High-level idea
----------------

1. The backend constructs a DAFSA from an internal word list (in `backend/app.py`) and exposes JSON APIs to fetch the graph, search words, report stats, add words, and trigger an automated DAFSA minimization (compression).
2. The frontend loads the DAFSA data to visualize the automaton and to power the game. The Phaser game fetches a word list from the backend and spawns collectible letters. When the player collects the correct letter sequence, the level progresses.
3. Adding words (via the game's menu or the React inspector) pushes words into the backend DAFSA. Calling the minimize endpoint triggers a structural compression of the DAFSA (merging equivalent states), simplifying the automaton and reducing state counts.
4. The GIF conversion utility converts animated GIFs into a horizontal spritesheet plus a JSON atlas compatible with Phaser's `load.atlas()`.

Core files
----------

- Backend: [backend/app.py](backend/app.py) — Flask server; DAFSA implementation and API endpoints:
  - `/api/levels` — returns the word list used by the game.
  - `/api/search?word=...` — checks whether a word exists in the current DAFSA.
  - `/api/stats` — returns counts for states and transitions.
  - `/api/graph` — returns the graph JSON used by the frontend visualizer.
  - `/api/minimize` (POST) — runs the DAFSA minimize routine and returns `before` and `after` stats.
  - `/api/add_word` (POST) — add a new word into the DAFSA (payload: `{ "word": "example" }`).

- GIF → spritesheet: [backend/convert_gif_to_spritesheet.py](backend/convert_gif_to_spritesheet.py) — uses Pillow to extract frames and write an atlas JSON suitable for Phaser.

- Frontend:
  - `frontend/` — Vite + React app.
  - `frontend/src/game/` — Phaser scenes: `BootScene`, `PreloadScene`, `GameScene`, `MenuScene`, `GraphScene`, `FailScene`, `WordCompleteScene`.
  - `frontend/src/Components/DafsaInspector.jsx` — small React UI to inspect/add/minimize the DAFSA.
  - `frontend/public/assets/` — game assets (example: `player_spritesheet.png`, `player_spritesheet.json`, letter images, background).

Quick start (development)
-------------------------

Prerequisites
- Node.js (16+ recommended) and npm
- Python 3.8+ and `pip`
- Optional: `ffmpeg`/`imagemagick` are NOT required for the provided converter (the converter uses Pillow). Install Pillow via `pip`.

Run backend (API)

1. Create & activate a Python virtual environment (recommended):

```bash
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

2. Install required Python packages:

```bash
pip install flask flask-cors pillow
```

3. Start the backend server (from repo root):

```bash
python backend/app.py
```

By default the Flask app runs on `http://127.0.0.1:5000` and exposes the DAFSA APIs described above.

Run frontend (dev)

1. Install frontend dependencies and run Vite (open a second terminal):

```bash
cd frontend
npm install
npm run dev
```

2. Vite will start a dev server (usually `http://localhost:5173`). The frontend expects the backend API at the same origin `/api/*` paths — when developing locally using Vite you can run both servers simultaneously (Vite proxies relative requests to the backend if configured), or use the absolute backend URL in the loader modules (see `frontend/src/dafsa/loadDafsa.js` which points at `http://127.0.0.1:5000/api/graph`).

Converting a GIF to a Phaser spritesheet
---------------------------------------

The repository includes a small utility that converts an animated GIF into a single horizontal spritesheet and a JSON atlas compatible with Phaser's `load.atlas()`.

Usage example:

```bash
python backend/convert_gif_to_spritesheet.py path/to/player.gif
```

This will create `player_spritesheet.png` and `player_spritesheet.json` next to the GIF. To use in the game:

1. Move the generated `player_spritesheet.png` and `player_spritesheet.json` into `frontend/public/assets/`.
2. Make sure `PreloadScene` loads the atlas: it already calls `this.load.atlas('player', 'assets/player_spritesheet.png', 'assets/player_spritesheet.json')`.
3. If the JSON/PNG filenames differ, update the arguments in `PreloadScene.js`.

How the game uses the DAFSA
---------------------------

- The backend builds a DAFSA from the `WORDS` list inside `backend/app.py`. The frontend's `GameScene` requests the word list at `/api/levels` and uses those words as level targets.
- While playing the user collects letter sprites. Successful completion of the full target word increases score and advances the word index.
- From the menu (or React inspector) you can add a new word. The backend `api_add_word` appends that word and updates the in-memory DAFSA.
- The frontend `MenuScene` also offers a “Minimize DAFSA” action which calls `/api/minimize` (POST). The backend's minimize routine groups equivalent states by signature and merges them — you will see `states`/`transitions` counts fall after minimization.
- The `GraphScene` fetches `/api/graph` and renders a visual representation of states and transitions; use it to observe how the DAFSA changes as words are added and minimized.

Notes, tips and known quirks
---------------------------

- `frontend/src/Components/DafsaInspector.jsx` currently attempts to POST to `/api/add_words` (plural) — but the backend exposes `/api/add_word` (singular). Use the in-game Menu to add words or update the component to POST to `/api/add_word`.
- The backend DAFSA is in-memory only — restarting `backend/app.py` rebuilds the DAFSA from the original `WORDS` list. If you need persistence, add a save/load layer (JSON file or a small DB).
- The GIF converter uses Pillow (`PIL`). For large GIFs ensure you have enough memory; you can also pass `frame_width` and `frame_height` when calling the script to force dimensions.

API examples
------------

Fetch graph JSON (used by the visualizer):

```bash
curl http://127.0.0.1:5000/api/graph
```

Add a word (POST JSON):

```bash
curl -X POST -H "Content-Type: application/json" -d '{"word":"example"}' http://127.0.0.1:5000/api/add_word
```

Trigger minimize and see before/after stats:

```bash
curl -X POST http://127.0.0.1:5000/api/minimize
```

Developer notes
---------------

- The DAFSA minimization implemented in `backend/app.py` groups states by a signature derived from outgoing transitions and final-flag and merges identical ones. That is sufficient for small demo datasets but is not a production-grade Hopcroft-style minimizer — it works bottom-up using computed heights and canonicalization.
- The frontend game is built with Phaser 3 and expects a horizontal spritesheet atlas. PreloadScene shows where the assets are loaded. The game logic in GameScene is simple: it spawns letter collectibles based on the current target word and checks for correct collection order.

Enjoy exploring the DAFSA + game interaction!
