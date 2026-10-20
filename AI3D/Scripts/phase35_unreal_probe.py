import unreal,json
from pathlib import Path
p=Path(unreal.Paths.project_dir()).resolve()
r={"project":str(p),"engine":unreal.SystemLibrary.get_engine_version()}
for name in ["StaticMeshEditorSubsystem","MaterialEditingLibrary","AutomationLibrary","EditorLevelLibrary","EditorLevelSubsystem","AssetCompilingManager","EditorAssetSubsystem"]:
 c=getattr(unreal,name,None);r[name]=[v for v in dir(c) if not v.startswith("_")] if c else None
(p/"probe.json").write_text(json.dumps(r,indent=2),"utf-8")
