"""Executed by the real Unreal Editor. No external project/content access."""
import unreal
import os,json,hashlib,traceback,time
from pathlib import Path

C=json.loads(Path(os.environ['GAME3D_PILOT_MANIFEST']).read_text('utf-8'))
PROJECT=Path(unreal.Paths.project_dir()).resolve();EXPECTED=Path(C['project']).resolve()
if PROJECT!=EXPECTED or json.loads((PROJECT/'.game3dcreator-pilot.json').read_text('utf-8'))['owner']!='game3dcreator-phase35':raise RuntimeError('Pilot project isolation failed')
RESULT=Path(C['result_path']);MODE=C['mode'];SM=C['mesh_path'];MAT=C['material_path']
if not all(p.startswith('/Game/Phase35/') for p in [SM,MAT,C['level_path'],*C['texture_paths'].values()]):raise RuntimeError('Content namespace outside isolated pilot')
TOOLS=unreal.AssetToolsHelpers.get_asset_tools();ASSETS=unreal.get_editor_subsystem(unreal.EditorAssetSubsystem)
STATIC=unreal.get_editor_subsystem(unreal.StaticMeshEditorSubsystem);LEVEL=unreal.EditorLevelLibrary
def canon(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def write(value):RESULT.parent.mkdir(parents=True,exist_ok=True);RESULT.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')
def xyz(v):return [float(v.x),float(v.y),float(v.z)]
def task(filename,path,options=None,factory=None):
    t=unreal.AssetImportTask();t.set_editor_property('filename',filename);t.set_editor_property('destination_path',path.rsplit('/',1)[0]);t.set_editor_property('destination_name',path.rsplit('/',1)[1])
    for k,v in [('automated',True),('replace_existing',True),('replace_existing_settings',True),('save',True)]:t.set_editor_property(k,v)
    if options:t.set_editor_property('options',options)
    if factory:t.set_editor_property('factory',factory)
    TOOLS.import_asset_tasks([t]);value=ASSETS.load_asset(path)
    if not value:raise RuntimeError('Import did not create '+path+'; results '+str(t.get_editor_property('imported_object_paths')))
    return value

def import_assets():
    unreal.SystemLibrary.execute_console_command(None,'Interchange.FeatureFlags.Import.FBX 0')
    ui=unreal.FbxImportUI()
    for k,v in [('import_mesh',True),('import_as_skeletal',False),('import_materials',False),('import_textures',False),('automated_import_should_detect_type',False),('mesh_type_to_import',unreal.FBXImportType.FBXIT_STATIC_MESH)]:ui.set_editor_property(k,v)
    data=ui.get_editor_property('static_mesh_import_data')
    for k,v in [('combine_meshes',True),('auto_generate_collision',False),('generate_lightmap_u_vs',False),('convert_scene',True),('convert_scene_unit',True),('force_front_x_axis',False),('import_uniform_scale',1.0)]:data.set_editor_property(k,v)
    mesh=task(C['sources']['fbx']['path'],SM,ui,unreal.FbxFactory())
    for i,name in [(1,'lod1'),(2,'lod2')]:
        value=STATIC.import_lod(mesh,i,C['sources'][name]['path'])
        if value!=i:raise RuntimeError('LOD import failed '+str(i))
    STATIC.remove_collisions(mesh);STATIC.add_simple_collisions(mesh,unreal.ScriptingCollisionShapeType.BOX)
    mat=ASSETS.load_asset(MAT)
    if not mat:mat=TOOLS.create_asset(MAT.rsplit('/',1)[1],MAT.rsplit('/',1)[0],unreal.Material,unreal.MaterialFactoryNew())
    unreal.MaterialEditingLibrary.delete_all_material_expressions(mat)
    for name,prop,out in [('albedo',unreal.MaterialProperty.MP_BASE_COLOR,'RGB'),('roughness',unreal.MaterialProperty.MP_ROUGHNESS,'R'),('metallic',unreal.MaterialProperty.MP_METALLIC,'R')]:
        tex=task(C['sources'][name]['path'],C['texture_paths'][name]);tex.set_editor_property('srgb',name=='albedo')
        tex.set_editor_property('compression_settings',unreal.TextureCompressionSettings.TC_DEFAULT if name=='albedo' else unreal.TextureCompressionSettings.TC_MASKS)
        sample=unreal.MaterialEditingLibrary.create_material_expression(mat,unreal.MaterialExpressionTextureSample,-450,{'albedo':-200,'roughness':0,'metallic':200}[name]);sample.set_editor_property('texture',tex)
        sample.set_editor_property('sampler_type',unreal.MaterialSamplerType.SAMPLERTYPE_COLOR if name=='albedo' else unreal.MaterialSamplerType.SAMPLERTYPE_MASKS)
        if not unreal.MaterialEditingLibrary.connect_material_property(sample,out,prop):raise RuntimeError('Material connection failed '+name)
        ASSETS.save_loaded_asset(tex,False)
    unreal.MaterialEditingLibrary.recompile_material(mat)
    for i in range(len(mesh.get_editor_property('static_materials'))):mesh.set_material(i,mat)
    ASSETS.save_loaded_asset(mat,False);ASSETS.save_loaded_asset(mesh,False)
    LEVEL.new_level(C['level_path'])
    actors=unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    actor=actors.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,0),unreal.Rotator(0,0,0));actor.set_actor_label('SM_PilotAsset')
    actor.static_mesh_component.set_static_mesh(mesh);actor.set_actor_scale3d(unreal.Vector(1,1,1))
    floor=actors.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(0,0,-12));floor.set_actor_label('PilotGround_NotAssetGeometry')
    floor.static_mesh_component.set_static_mesh(ASSETS.load_asset('/Engine/BasicShapes/Cube'));floor.set_actor_scale3d(unreal.Vector(22,22,.2))
    cube=actors.spawn_actor_from_class(unreal.StaticMeshActor,unreal.Vector(600,-500,50));cube.set_actor_label('100cm_ReferenceCube_NotGeneratedAsset')
    cube.static_mesh_component.set_static_mesh(ASSETS.load_asset('/Engine/BasicShapes/Cube'))
    sun=actors.spawn_actor_from_class(unreal.DirectionalLight,unreal.Vector(0,0,1000),unreal.Rotator(-48,-35,0));sun.light_component.set_editor_property('intensity',4.0)
    sky=actors.spawn_actor_from_class(unreal.SkyLight,unreal.Vector(0,0,800));sky.light_component.set_editor_property('source_type',unreal.SkyLightSourceType.SLS_SPECIFIED_CUBEMAP)
    # Known engine cubemap; absence is explicit, not replaced with an invented map.
    cubemap=ASSETS.load_asset('/Engine/EngineResources/DefaultTextureCube')
    if cubemap:sky.light_component.set_editor_property('cubemap',cubemap)
    sky.light_component.set_editor_property('intensity',1.0)
    camera=actors.spawn_actor_from_class(unreal.CameraActor,unreal.Vector(1200,-1400,2400));camera.set_actor_label('PilotCamera_50deg')
    camera.set_actor_rotation(unreal.MathLibrary.find_look_at_rotation(camera.get_actor_location(),unreal.Vector(0,0,200)),False)
    camera.camera_component.set_editor_property('projection_mode',unreal.CameraProjectionMode.PERSPECTIVE)
    camera.camera_component.set_editor_property('field_of_view',35.0)
    camera.camera_component.set_editor_property('aspect_ratio',1.0)
    camera.camera_component.set_editor_property('constrain_aspect_ratio',True)
    LEVEL.save_current_level();ASSETS.save_directory('/Game/Phase35',False,True)

def inspect():
    mesh=ASSETS.load_asset(SM);mat=ASSETS.load_asset(MAT)
    if not mesh or not mat:raise RuntimeError('Saved pilot assets missing')
    bounds=mesh.get_bounds();origin=xyz(bounds.origin);extent=xyz(bounds.box_extent)
    material={}
    for name,prop in [('albedo',unreal.MaterialProperty.MP_BASE_COLOR),('roughness',unreal.MaterialProperty.MP_ROUGHNESS),('metallic',unreal.MaterialProperty.MP_METALLIC)]:
        node=unreal.MaterialEditingLibrary.get_material_property_input_node(mat,prop);tex=node.get_editor_property('texture') if node else None
        material[name]={'connected_texture':tex.get_path_name().split('.')[0] if tex else None,'srgb':tex.get_editor_property('srgb') if tex else None,
                        'compression':str(tex.get_editor_property('compression_settings')) if tex else None,'output':unreal.MaterialEditingLibrary.get_material_property_input_node_output_name(mat,prop)}
    actors=unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
    actor=next((a for a in actors if a.get_actor_label()=='SM_PilotAsset'),None)
    import_data=mesh.get_editor_property('asset_import_data')
    settings={'import_data_class':import_data.get_class().get_name()}
    for k in ['convert_scene','convert_scene_unit','force_front_x_axis','import_uniform_scale']:
        try:settings[k]=import_data.get_editor_property(k)
        except Exception:settings[k]=None
    return {'evidence_origin':'unreal_editor_python_api','mode':MODE,'execution_nonce':C['execution_nonce'],'project':str(PROJECT),'engine_version':unreal.SystemLibrary.get_engine_version(),
        'editor_command_line':unreal.SystemLibrary.get_command_line(),
        'source_manifest_sha256':hashlib.sha256(canon(C['sources']).encode()).hexdigest(),'import_performed':MODE=='import',
        'mesh_path':mesh.get_path_name(),'mesh_class':mesh.get_class().get_name(),'lod_count':STATIC.get_lod_count(mesh),
        'lod_triangles':[mesh.get_num_triangles(i) for i in range(STATIC.get_lod_count(mesh))],
        'lod_screen_sizes':list(STATIC.get_lod_screen_sizes(mesh)),'simple_collision_count':STATIC.get_simple_collision_count(mesh),
        'dimensions_cm':[x*2 for x in extent],'bounds_origin_cm':origin,'bounds_min_cm':[origin[i]-extent[i] for i in range(3)],
        'material_slots':[s.material_interface.get_path_name().split('.')[0] if s.material_interface else None for s in mesh.get_editor_property('static_materials')],
        'material':material,'actor_scale':xyz(actor.get_actor_scale3d()) if actor else None,'actor_rotation':str(actor.get_actor_rotation()) if actor else None,
        'import_settings':settings,'saved':all(ASSETS.does_asset_exist(p) for p in [SM,MAT,*C['texture_paths'].values()]),
        'shader_compilation_verified':False,'screenshot_is_blender':False,'automatic_approval':False}

def render():
    # UE5.7's -ExecutePythonScript executor otherwise closes the Editor on the
    # next frame when this async setup returns, before any screenshot can run.
    unreal.EditorPythonScripting.set_keep_python_script_alive(True)
    LEVEL.load_level(C['level_path']);actors=unreal.get_editor_subsystem(unreal.EditorActorSubsystem).get_all_level_actors()
    camera=next(a for a in actors if a.get_actor_label()=='PilotCamera_50deg');target=Path(C['screenshot_dir']);target.mkdir(parents=True,exist_ok=True)
    world=unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world()
    unreal.get_editor_subsystem(unreal.EditorActorSubsystem).clear_actor_selection_set()
    unreal.AutomationLibrary.set_editor_viewport_view_mode(unreal.ViewModeIndex.VMI_LIT)
    unreal.AutomationLibrary.set_editor_active_viewport_view_mode(unreal.ViewModeIndex.VMI_LIT)
    LEVEL.editor_set_game_view(True);LEVEL.editor_invalidate_viewports()
    for command in ['r.Streaming.PoolSize 1000','r.SetRes 1024x1024w','r.ScreenPercentage 100','r.DefaultFeature.AutoExposure 0','r.MotionBlurQuality 0']:
        unreal.SystemLibrary.execute_console_command(world,command)
    unreal.AutomationLibrary.finish_loading_before_screenshot()
    state={'started':time.monotonic(),'index':0,'task':None,'requested':False,'files':[]};mesh=ASSETS.load_asset(SM)
    actor=next(a for a in actors if a.get_actor_label()=='SM_PilotAsset')
    def tick(delta):
        try:
            elapsed=time.monotonic()-state['started']
            if elapsed>240:raise RuntimeError('Screenshot completion timeout; no fake evidence')
            if elapsed<12:return
            i=state['index']
            if not state['requested']:
                actor.static_mesh_component.set_forced_lod_model(i+1)
                unreal.AutomationLibrary.set_editor_active_viewport_view_mode(unreal.ViewModeIndex.VMI_LIT)
                if unreal.AutomationLibrary.get_editor_active_viewport_view_mode()!=unreal.ViewModeIndex.VMI_LIT:raise RuntimeError('Lit viewport not verified')
                print('PHASE35 real Unreal screenshot request LOD'+str(i),flush=True)
                state['task']=unreal.AutomationLibrary.take_high_res_screenshot(1024,1024,str(target/('unreal-lod'+str(i)+'.png')),camera,delay=2.0)
                state['requested']=True;return
            path=target/('unreal-lod'+str(i)+'.png')
            if path.is_file() and path.stat().st_size>1000 and state['task'].is_task_done():
                state['files'].append(str(path));state['index']+=1;state['requested']=False
                if state['index']==3:
                    value=inspect();value.update(screenshot_paths=state['files'],screenshot_method='Unreal AutomationLibrary.take_high_res_screenshot with real RHI',
                        shader_loading_finish_called=True,viewport_view_mode=str(unreal.AutomationLibrary.get_editor_active_viewport_view_mode()),
                        camera_aspect_ratio=camera.camera_component.get_editor_property('aspect_ratio'),
                        forced_lod_models=[1,2,3],lod_preview_mode='forced LOD0/LOD1/LOD2; continuous gameplay transitions not tested',camera_rotation=str(camera.get_actor_rotation()),
                        camera_rotation_degrees=[camera.get_actor_rotation().pitch,camera.get_actor_rotation().yaw,camera.get_actor_rotation().roll])
                    write(value);unreal.unregister_slate_post_tick_callback(HANDLE);unreal.EditorPythonScripting.set_keep_python_script_alive(False);unreal.SystemLibrary.quit_editor()
        except Exception as e:
            write({'error':{'message':str(e),'traceback':traceback.format_exc()}});unreal.unregister_slate_post_tick_callback(HANDLE);unreal.EditorPythonScripting.set_keep_python_script_alive(False);unreal.SystemLibrary.quit_editor()
    global HANDLE
    HANDLE=unreal.register_slate_post_tick_callback(tick)

try:
    if MODE=='import':import_assets();write(inspect());unreal.SystemLibrary.quit_editor()
    elif MODE=='reload':LEVEL.load_level(C['level_path']);write(inspect());unreal.SystemLibrary.quit_editor()
    elif MODE=='render':render()
    else:raise ValueError('Unknown pilot mode')
except Exception as e:
    write({'error':{'message':str(e),'traceback':traceback.format_exc()},'project':str(PROJECT),'mode':MODE})
    unreal.SystemLibrary.quit_editor()
