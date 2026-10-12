# docs/media

Images and video used by the READMEs.

| File | What it is |
|---|---|
| `logo.svg` | The console logo (copy of `datamind_console/ui/assets/datamind_logo_blue.svg`) |
| `hero-route-map.webp` | Animated preview cut from the console tour: one route direction and its stops on the map, then a zoom (1100 px, 12 fps). The map attribution is redrawn in the corner because the crop leaves out the map's own attribution line |
| `datamind-console-demo.mp4` | 70-second console tour, no audio: route map → re-entry before/after → review queues → GTFS validation gate → AI scope |
| `en/*.jpg`, `es/*.jpg` | Console screenshots per UI language, sidebar cropped out: `before-after`, `synthetic-stops`, `gtfs-validation`, `ai-scope`, `overview` |

All of them are captures of the real console (October 2026) running on the original project's private data in Ecuador, with every database connection read-only. Apart from that attribution line they are cropped and resized only: nothing else is drawn on top, and no frame shows hostnames, credentials or people's names. Map frames keep the © CARTO / © OpenStreetMap contributors attribution.

This repository ships no real data. Running it locally shows the synthetic sample instead.
