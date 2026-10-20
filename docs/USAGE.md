# Using Local Asset Studio

## Reference design

Open `/`. Choose a catalog item or create an asset from a prompt. Specify the intended object, material, dimensions and polygon budget. References should contain only the object, on a plain background, with no ground patch or display base. Adjust the prompt/components and generate another reference until it matches your intent.

Ordinary reference preparation is immediate when the GPU is available, not a batch image queue. Select suitable references and add them to 3D production. The web human-selection flow does not add a semantic VLM reference gate before Shape; actual file/mask integrity and GPU locks remain required.

## Production queue

Open `/queue`. The same board appears in the main workbench.

- **Active:** asset name, requested revision, actual stage, elapsed stage time and links.
- **Waiting:** ready manual 3D selections and automatic revisions, including those without a generated reference/child job yet.
- **Move up/down:** update the same persisted order the dispatcher consumes. Stale lists are rejected; refresh before retrying.
- **Remove:** cancel a waiting entry without deleting old outputs. A cancelled review is not silently re-enrolled.
- **Pause:** let the current job finish and hold later work. Resume to continue the queue.
- **Results:** inspect real failure reasons, the current model, and production details. Geometry blocked before PBR may be untextured; this is not a completed textured delivery.

The browser can be closed; the server and worker must remain running. The board polls every four seconds. Elapsed times are measured; no reliable completion ETA is claimed.

## Model decisions

Open `/reviews`. Use orbit/zoom/pan, isometric/front/top, wireframe or clay mode. Source references, technical reports, visual scores and available files are shown beside the model.

**Accept** records your judgment of the exact model bytes. Raw geometry, missing delivery stages, failed validation or a low VLM score do not veto human acceptance. Technical results remain unchanged: accepting raw geometry does not invent PBR/FBX/validation files, complete the production job, override GPU locks or start additional inference. Missing/changed files, unreadable geometry and concurrent active mutation still require resolution.

**Request revision** and **Reject** require a note. The note is resolved into desired final design requirements with the existing CPU model. A new reference and 3D candidate are generated automatically under that explicit review authorization. The previous files and decision history remain intact. A single review creates at most one new production job, and a failure stops that attempt instead of opening unlimited new IDs.

Notes such as “not good” request a new variant without identifying a specific correction. A precise note works better. This is prompt-based regeneration, not deterministic editing of the old mesh. Review the new candidate yourself.

## Files and game integration

Actual completed stages determine which files exist. Successful packages can include `model.glb`, `model.fbx`, `model.obj`, `model.blend`, PBR maps, LODs, previews and JSON reports. Do not treat absent normal/AO or LOD files as delivered.

There is no preset game style or asset catalog. Start from your own prompt and optional style, or configure a private JSON catalog with `LAS_CATALOG_PATH`. Outputs stay under `AI3D/Outputs`, with state under `AI3D/State` and separate reports under `AI3D/Quality`. Back up those folders before making operational changes. No automatic import into a main Unreal project occurs.
