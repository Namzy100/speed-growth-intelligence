---
name: youtube-broll-maker
description: Generate 10-second B-roll footage of yourself doing an action using the Higgsfield MCP (Seedance 2.0). Upload one photo of yourself, describe the action ("typing on laptop", "walking at night"), pick from 5 cinematography style presets, get an MP4. Use when the user wants B-roll, cutaway footage, "me doing X" video, YouTube B-roll, or any image-to-video of themselves performing an action.
argument-hint: [path to photo of yourself] "[action you want to be doing]"
---

# B-Roll Maker

You generate 10-second B-roll footage of the user doing an action, using the **Higgsfield MCP** (Seedance 2.0). All generation happens through Higgsfield — never use local video tools.

## Required Inputs

1. **Photo of the user** — a local file path or attached image. If missing, ask once and stop.
2. **Action description** — short phrase like "typing on a laptop", "walking down a city street at night", "drinking coffee at a desk". If missing, ask once.
3. **Style preset** — one of the 5 below, chosen via `AskUserQuestion`. Never pick for the user.

## Workflow

### 1. Load Higgsfield MCP tools via ToolSearch
Deferred MCP tools — must load before calling:
```
select:mcp__claude_ai_Higgsfield__media_upload,mcp__claude_ai_Higgsfield__media_confirm,mcp__claude_ai_Higgsfield__generate_video,mcp__claude_ai_Higgsfield__job_status,mcp__claude_ai_Higgsfield__job_display,mcp__claude_ai_Higgsfield__models_explore,mcp__claude_ai_Higgsfield__balance
```

### 2. Collect inputs
- Parse `$ARGUMENTS` and the user's message for the photo path + action description.
- Present the 5 style presets via `AskUserQuestion` (single-select). Each option's `label` is the preset name and `description` is the vibe summary (see table below).

### 3. Upload the photo to Higgsfield
- `media_upload` with `filename` (basename) and `content_type` (image/jpeg, image/png, or image/webp matching the file).
- PUT the bytes to the returned `upload_url`:
  ```
  curl --upload-file "<local_path>" "<upload_url>"
  ```
  (or run the curl command provided in the response verbatim).
- `media_confirm` with `media_id: <returned id>`, `type: "image"`. Capture the `media_id` UUID.

### 4. Generate the video
Resolve the prompt template from the chosen preset (substitute the user's action at `<ACTION>`), then call `generate_video`:
```
params:
  model: "seedance_2_0"
  prompt: "<resolved preset prompt>"
  duration: 10
  aspect_ratio: "16:9"
  resolution: "<preset resolution>"
  genre: "<preset genre>"
  count: 1
  medias:
    - { value: "<media_id>", role: "image" }
```
Optionally call with `get_cost: true` once first to preflight credit cost and tell the user before submitting the real job.

### 5. Poll for the result
- Call `job_status` with the returned `jobId` and `sync: true` — server polls internally up to ~25s.
- Seedance videos typically take 60–180s. If still pending after the sync call, use `ScheduleWakeup` at `delaySeconds: 120` (then respect `poll_after_seconds` on subsequent checks). Do not busy-poll.

### 6. Deliver
Return the MP4 URL as a clickable link. Include the chosen preset name **and the resolved prompt verbatim** so the user can tweak and re-run. Optionally call `job_display` with the `jobId` to surface the result in the Higgsfield UI widget.

## The 5 Style Presets

| # | Name | Vibe | `genre` | `resolution` | Prompt template (substitute `<ACTION>`) |
|---|------|------|---------|--------------|------------------------------------------|
| 1 | **Cinematic Anamorphic** | Moody, shallow DOF, teal-and-orange film grade, slow dolly-in | `drama` | `1080p` | `Cinematic anamorphic shot, shallow depth of field, teal-and-orange color grade, soft volumetric backlight, slow dolly-in. The person <ACTION>. 35mm film texture, premium feature-film feel, identity preserved.` |
| 2 | **Documentary Vlog** | Natural light, handheld, observational vérité | `auto` | `720p` | `Observational documentary B-roll, natural window light, handheld with subtle camera sway, true-to-life colors. The person <ACTION>. Candid, unstaged, authentic vlog feel, identity preserved.` |
| 3 | **Tech Studio** | Dark room, RGB / neon accents, glossy desk, smooth slider | `action` | `1080p` | `Modern tech-creator studio B-roll, dark room with RGB and neon accents, glossy desk, glass and metal surfaces, smooth motorized slider move, crisp focus. The person <ACTION>. High-end YouTube tech-channel aesthetic, identity preserved.` |
| 4 | **Golden Hour Lifestyle** | Warm sunlight, lens flare, gentle bokeh, Apple-ad clean | `auto` | `1080p` | `Golden hour lifestyle B-roll, warm low-angle sunlight through window, soft lens flare, creamy bokeh background, gentle slider movement. The person <ACTION>. Aspirational, clean, Apple-ad aesthetic, identity preserved.` |
| 5 | **Moody Mono Film** | High-contrast black & white, 35mm grain, contemplative | `noir` | `1080p` | `High-contrast black-and-white 35mm film B-roll, deep shadows, soft key light, visible film grain, slow contemplative push-in. The person <ACTION>. Editorial, introspective, festival-short-film tone, identity preserved.` |

## Critical Rules

1. Always use the **Higgsfield MCP** — never ffmpeg, local AI video tools, or anything else.
2. Always load deferred MCP tools via ToolSearch before calling them.
3. Always upload the photo via `media_upload` → curl PUT → `media_confirm`. Never pass a local file path to `generate_video`.
4. Always use `model: "seedance_2_0"`, `duration: 10`, `aspect_ratio: "16:9"` unless the user explicitly overrides.
5. Use `image` role for the photo (identity reference) — not `start_image`. This gives more dynamic B-roll motion.
6. Never invent the action description or pick the style preset — ask once for each if missing.
7. Surface the chosen preset's resolved prompt verbatim in the final delivery so the user can tweak and re-run.
8. Don't busy-poll `job_status` — use `sync: true` first, then `ScheduleWakeup` at 120s+ if still pending.

## Quick Template — Final Delivery

```
B-roll ready — "<ACTION>" · preset: <Preset Name>

<mp4_url>

Prompt used:
"<resolved prompt>"

(Higgsfield job: <jobId> · ~10s · 16:9 · <resolution>)
```

Use `$ARGUMENTS` for the photo path + action when present.
