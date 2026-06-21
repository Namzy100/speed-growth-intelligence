---
name: youtube-popup-graphic
description: Generate stunning Adobe-Premiere-Pro-style UI popup motion graphics for YouTube videos using the Higgsfield MCP. The user uploads a style reference image (any UI mockup, dashboard, glass card, design they love), names one skill/feature/item, and the skill produces an MP4 of a single card popping in styled to match the reference. Use when the user wants a "popup graphic", "popup animation", "card popup", "skill popup", "AE/Premiere-style animation", "stunning UI animation for YouTube", or says "make me a popup for <skill/feature/item> styled like <reference>".
argument-hint: [item name — e.g. "AI Clipper"] [optional: path to style reference image]
---

# YouTube Popup Graphic

You produce **stunning, Adobe-Premiere-Pro-style UI popup motion graphics** for YouTube — a single card popping into frame in the **exact visual style of a user-supplied UI reference image**. All generation happens through the **Higgsfield MCP**. Never use local image/video tools (no ffmpeg, ImageMagick, Pillow, yt-dlp, etc.).

## The pipeline (this is the new method — battle-tested)

The shortest path to a premium popup is also the best one. Stop trying to be clever:

1. **User uploads a UI/style reference image** (dashboard, glass card, dark-mode UI mockup, whatever they like — this defines the *theme*).
2. **Upload that image directly to Higgsfield** via `media_upload` + curl PUT + `media_confirm`.
3. **Call `generate_video` with `seedance_2_0`** passing the reference with `role: "image"` (style anchor) and a **short** prompt. That's it.

What we explicitly do NOT do anymore:
- ❌ No `nano_banana_2` intermediate image generation. It added a step that didn't improve the result.
- ❌ No `start_image` + `end_image` interpolation. It produced stiff PowerPoint-style A→B animations.
- ❌ No 20-line over-specified prompt with letter-by-letter spelling guards. Video diffusion models can't read that detail anyway and a short prompt + a strong reference beats a long brief every time.
- ❌ No multi-card layouts in one generation. Three text labels in one 8-second shot reliably get garbled by every video model (Veo, seedance, kling). One card per popup — stitch later if needed.

## Defaults (do not re-ask)

- **Model**: `seedance_2_0`
- **Aspect ratio**: `16:9` (only override for Shorts `9:16` or square `1:1`)
- **Duration**: `8` seconds
- **Resolution**: `1080p`
- **Genre**: `auto`
- **Reference role**: `image` (style anchor — NOT `start_image`, NOT `end_image`)
- **Audio direction**: SFX-only by default. The model auto-generates audio; if you don't direct it, it adds music.

## Required Inputs (gather up front — never guess)

1. **Item name** — the single skill/feature/product name to render on the card. From `$ARGUMENTS` or the user's message.
2. **Style reference image** — a local file path. **This is required.** The user is meant to drag-drop *any UI picture they want the theme to match*: dashboard screenshot, dribbble UI mockup, glass card, dark-mode app, fintech UI — anything visual.
3. (Optional) Audio preference — default is SFX-only / no music. Only switch if user says otherwise.

If multiple items are requested in one call: do not pack them into one shot. **Generate N separate popup videos in parallel**, one per item, and surface all N URLs. Mention that they can stitch in post.

## Workflow

### 1. Gather context

a) **Item name** from `$ARGUMENTS` or the message. If only a topic was given (e.g. "make popups for my Claude skills"), ask which item — but if a name is clearly inferable from recent conversation, just use it.

b) **Reference image path** — if not in `$ARGUMENTS` or recent message, ask once: *"Drop the path to a UI/style reference image — any dashboard, glass card, dark-mode mockup, or design whose look you want the popup to match."* Stop until provided.

### 2. Load deferred Higgsfield MCP tools via ToolSearch

Single call:
```
select:mcp__claude_ai_Higgsfield__media_upload,mcp__claude_ai_Higgsfield__media_confirm,mcp__claude_ai_Higgsfield__generate_video,mcp__claude_ai_Higgsfield__job_display,mcp__claude_ai_Higgsfield__balance
```

(Image-gen tools are not needed in this pipeline — drop them from the load.)

### 3. Preflight credits

Call `balance`. One seedance 8s 1080p video is cheap (typically ~12–30 credits); if the wallet is short, stop and tell the user the shortfall.

### 4. Upload the style reference

`media_upload` with the reference (one entry in `files[]`, `content_type` matching the extension).

For the returned `upload_url`, PUT the bytes:
```bash
curl -X PUT -H "Content-Type: <mime>" --data-binary @<local_path> "<upload_url>"
```
(Use the curl line the response provides, verbatim — content-type must match what was uploaded.)

Then `media_confirm` with `media_ids: [<id>]`, `type: "image"`. Capture the `media_id` — this is the `reference_media_id`.

### 5. Generate the video

`generate_video`:
```
model: "seedance_2_0"
aspect_ratio: "16:9"        # or user override
duration: 8                  # or user override
resolution: "1080p"
genre: "auto"
count: 1
declined_preset_id: "..."    # if the server returns a preset_recommendation, decline it and retry literally
medias:
  - { value: <reference_media_id>, role: "image" }
prompt: |
  Create an Adobe Premiere Pro style popup animation for 1 <thing> called "<item name>".

  Use the reference image for the theme.

  A single card pops up cleanly into the center of the frame with a confident
  Premiere-Pro UI animation — clean rise and fade-in, sharp ease-out, premium
  broadcast feel. The card matches the reference image's theme exactly (colors,
  material, shadow, typography, accents). Inside the card: a small icon and the
  text "<item name>" — large, bold, perfectly spelled. Hold the final hero
  composition for the rest of the shot.

  Sound effects only — a soft whoosh as the card rises in, then a tactile snap
  when it lands. No background music, no soundtrack, no melody.
```

**Keep the prompt short like that.** Resist the urge to over-specify. The reference image is doing the heavy lifting on aesthetic; the prompt only needs to direct the popup mechanic + the text + the audio.

If the user said "shorts" / vertical / 9:16, set `aspect_ratio: "9:16"`.
If the user wants audio with music, drop the "Sound effects only…" sentence.
If the user wants a multi-element shot anyway, gently note that text rendering will be unreliable and recommend N separate popups.

### 6. Poll for the video

Seedance videos typically take 60–180s. Use background bash with an `until`-loop sleeping 60–90s, then call `job_display` to check. Don't busy-poll. When done, the result includes `results.rawUrl` — the MP4.

If the model returns a `preset_recommendation` notice instead of submitting (e.g. "IN THE DARK" preset), retry the call with `declined_preset_id: <that preset id>` to force literal generation.

### 7. Multi-item batching (only if user asked for multiple)

If the user asks for several items (e.g. "make popups for Thumbnail, AI Clipper, B-roll"):
- Do NOT pack them into one shot.
- Submit N separate `generate_video` jobs in parallel, each with the same reference image and the same short prompt template, swapping only the `<item name>` and `<thing>` slots.
- Wait for all N to finish (start one background timer per job or rely on `job_display` polling on each).
- Deliver all N URLs together. Tell the user they can splice them in post.

### 8. Deliver

```
Popup graphic ready — "<item name>"

<mp4_url>

Style reference: <reference path>
Prompt used:
"<resolved prompt>"

(Higgsfield job: <videoJobId> · 8s · 16:9 · 1080p · seedance_2_0)
```

For multi-item runs, list all N items and URLs.

Optionally call `job_display` with the video `jobId` to surface the result in the Higgsfield UI widget.

## Critical Rules

1. Always use the **Higgsfield MCP** — never ffmpeg, ImageMagick, Pillow, yt-dlp, or any local image/video tool.
2. Always load deferred Higgsfield MCP tools via ToolSearch in **one** call before invoking them.
3. **A style reference image is required.** Never proceed without one. If the user has none, ask them to drop a path before doing anything.
4. **Upload the reference DIRECTLY** as `role: "image"` on `seedance_2_0`. Do not run an intermediate `generate_image` step. Do not pass it as `start_image` or `end_image`.
5. **Keep the prompt short.** ~6–10 lines max. Trust the reference image to anchor the aesthetic.
6. **One item per shot.** For multi-item requests, run N separate generations in parallel — never one shot with multiple labels.
7. Always upload via `media_upload` → curl PUT → `media_confirm`. Never pass a local file path to `generate_video`.
8. SFX-only audio by default. seedance auto-generates audio; if you don't direct it, you'll get music — explicitly say "Sound effects only — no background music, no soundtrack, no melody" in the prompt.
9. If the server returns a `preset_recommendation`, retry with `declined_preset_id: <id>` to force literal generation.
10. Don't busy-poll `job_display` — use `until`-loop background bash with 60–90s sleeps, or rely on harness notifications.
11. Surface the resolved video prompt verbatim and the reference path in the delivery message so the user can iterate.

## Quick Template — Final Delivery

```
Popup graphic ready — "<item name>"

<mp4_url>

Style reference: <reference path>
Prompt used:
"<resolved prompt>"

(Higgsfield job: <videoJobId> · 8s · 16:9 · 1080p · seedance_2_0)
```

Use `$ARGUMENTS` as the item name (and reference path if a file path appears in it) when present.
