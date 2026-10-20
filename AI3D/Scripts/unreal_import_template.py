"""Optional future UE Editor Python import; no execution during installation.
Set SOURCE_FBX explicitly in Unreal's Python console before using this template.
"""
def import_generated_asset(source_fbx, destination='/Game/Generated/AI3D'):
    from pathlib import Path
    import unreal
    src=Path(source_fbx).resolve()
    if src.suffix.lower()!='.fbx' or not src.is_file():raise ValueError('Provide an existing generated FBX file')
    task=unreal.AssetImportTask()
    task.filename=str(src)
    task.destination_path=destination
    task.automated=True
    task.replace_existing=False
    task.save=False
    options=unreal.FbxImportUI()
    options.import_mesh=True
    options.import_as_skeletal=False
    options.import_materials=True
    options.import_textures=True
    task.options=options
    unreal.AssetToolsHelpers.get_asset_tools().import_asset_tasks([task])
    return task.imported_object_paths
