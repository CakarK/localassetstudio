"""CPU evidence only. Ratios/voxel shell occupancy are not physical enclosed volume."""
import json
from pathlib import Path
import numpy as np
import trimesh
from PIL import Image,ImageDraw
from scipy import ndimage
from .common import AI,digest

CONFIG=AI/'Config/quality_gates.json'

def profile_for(asset,override=None):
    config=json.loads(CONFIG.read_text('utf-8'));name=override or asset.get('phase35_gate_profile') or config['asset_profiles'].get(asset.get('asset_id'))
    if name is None:name='building' if asset.get('category')=='Buildings' else 'unknown'
    if name not in config['profiles']:raise ValueError('Unknown gate profile: '+name)
    return {'name':name,'version':config['version'],'config_sha256':digest(config),**config['profiles'][name]}

def check(name,status,detail):return {'check':name,'status':status,'detail':detail}
def decision(checks):return 'FAIL' if any(c['status']=='FAIL' for c in checks) else 'NeedsReview' if any(c['status']=='NeedsReview' for c in checks) else 'PASS'

def reference_analysis(reference,processed,profile):
    checks=[]
    with Image.open(reference) as image:original=np.array(image.convert('RGB'));source_size=image.size
    with Image.open(processed) as image:
        has_alpha='A' in image.getbands();rgba=np.array(image.convert('RGBA'));size=image.size
    checks.append(check('alpha_available','PASS' if has_alpha else 'NeedsReview','Explicit alpha channel is required to assess background removal.'))
    checks.append(check('matching_reference_dimensions','PASS' if size==source_size else 'FAIL',{'reference':source_size,'processed':size}))
    mask=rgba[:,:,3]>127;area=float(mask.mean());h,w=mask.shape
    if not mask.any():return {'status':'FAIL','checks':checks+[check('nonempty_foreground','FAIL','Mask is empty')],'metrics':{'foreground_fraction':area}}
    ys,xs=np.nonzero(mask);bbox=[int(xs.min()),int(ys.min()),int(xs.max()+1),int(ys.max()+1)]
    labels,n=ndimage.label(mask,structure=np.ones((3,3)));sizes=np.bincount(labels.ravel())[1:];largest=float(sizes.max()/sizes.sum())
    significant=int((sizes>=max(4,mask.sum()*.01)).sum());edge=np.concatenate([mask[0,:],mask[-1,:],mask[:,0],mask[:,-1]])
    offset=float(max(abs(xs.mean()/w-.5),abs(ys.mean()/h-.5)));limits=profile['reference']
    checks.append(check('category_foreground_area','PASS' if limits['min_area']<=area<=limits['max_area'] else 'NeedsReview',{'actual':area,'range':[limits['min_area'],limits['max_area']],'not_universal_acceptance_rule':True}))
    checks.append(check('mask_component_integrity','PASS' if largest>=limits['min_largest_component'] else 'NeedsReview',{'components':int(n),'significant_components':significant,'largest_foreground_fraction':largest}))
    checks.append(check('object_center','PASS' if offset<=limits['max_center_offset'] else 'NeedsReview',{'normalized_max_offset':offset}))
    checks.append(check('frame_edge_contact','PASS' if float(edge.mean())<=limits['max_edge_fraction'] else 'NeedsReview',{'perimeter_fraction':float(edge.mean()),'touches':[bool(mask[0].any()),bool(mask[-1].any()),bool(mask[:,0].any()),bool(mask[:,-1].any())]}))
    rgb_delta=float(np.abs(original.astype(float)-rgba[:,:,:3].astype(float))[mask].mean()) if size==source_size else None
    checks.append(check('foreground_rgb_preservation','PASS' if rgb_delta is not None and rgb_delta<=15 else 'NeedsReview',{'mean_absolute_rgb_delta':rgb_delta,'note':'Cannot prove removed pixels were background; VLM must compare both images.'}))
    if min(w,h)<128:checks.append(check('minimum_resolution','FAIL',size))
    if profile['name']=='unknown':checks.append(check('asset_class_known','NeedsReview','Explicit asset class rules unavailable'))
    return {'status':decision(checks),'checks':checks,'metrics':{'foreground_fraction':area,'bbox_xyxy':bbox,'bbox_fraction':[(bbox[2]-bbox[0])/w,(bbox[3]-bbox[1])/h],
        'centroid_normalized':[float(xs.mean()/w),float(ys.mean()/h)],'components':int(n),'significant_components':significant,'largest_component_fraction':largest,'edge_contact_fraction':float(edge.mean())}}

def geometry_analysis(source,profile,output=None):
    mesh=trimesh.load(source,force='mesh',process=False)
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():raise ValueError('Empty/nonfinite geometry')
    # Weld coordinates for connectivity analysis only; never write the mesh back.
    audit=trimesh.Trimesh(mesh.vertices.copy(),mesh.faces.copy(),process=True)
    size=audit.extents;long=max(float(size[0]),float(size[2]));small=min(float(size[0]),float(size[2]))
    if long<=0 or float(size[1])<=0:raise ValueError('Zero-size geometry')
    groups=trimesh.graph.connected_components(audit.face_adjacency,nodes=np.arange(len(audit.faces)),min_len=1)
    areas=np.array([audit.area_faces[g].sum() for g in groups]);total=float(areas.sum())
    if total<=0:raise ValueError('Zero surface area')
    distribution=sorted((float(x/total) for x in areas),reverse=True);significant=sum(x>=.01 for x in distribution)
    limit=profile['geometry'];ratios={'planar_min_over_max':small/long,'height_over_planar_max':float(size[1])/long}
    checks=[check('category_planar_ratio','PASS' if ratios['planar_min_over_max']>=limit['min_planar_ratio'] else 'FAIL',ratios),
            check('category_height_ratio','PASS' if limit['min_height_planar_ratio']<=ratios['height_over_planar_max']<=limit['max_height_planar_ratio'] else 'FAIL',ratios),
            check('component_surface_distribution','PASS' if distribution[0]>=limit['min_largest_area_fraction'] and significant<=limit['max_significant_components'] else 'NeedsReview',{'largest_fraction':distribution[0],'significant_components':significant})]
    covariance=np.cov(audit.vertices.T);eigenvalues=np.linalg.eigvalsh(covariance);pca_ratio=float(max(0,eigenvalues[0])/max(eigenvalues[-1],1e-15))
    # Orthographic raster coverage measures projected surface, not solid occupancy.
    projections={};resolution=128
    for name,axes in [('top',(0,2)),('front',(0,1)),('side',(2,1))]:
        pts=audit.vertices[:,axes];lo=pts.min(0);span=np.maximum(pts.max(0)-lo,1e-12);pixels=(pts-lo)/span*(resolution-3)+1
        image=Image.new('L',(resolution,resolution));draw=ImageDraw.Draw(image)
        for face in audit.faces:draw.polygon([tuple(p) for p in pixels[face]],fill=255)
        covered=np.asarray(image)>0;projections[name]={'covered_canvas_fraction':float(covered.mean()),'projection_axes':axes}
        if output:
            path=Path(output)/(name+'-silhouette.png');image.save(path);projections[name]['image']=str(path.resolve())
    points=np.vstack([audit.vertices[::max(1,len(audit.vertices)//75000)],audit.triangles_center[::max(1,len(audit.faces)//75000)]])
    quantized=np.floor((points-audit.bounds[0])/np.maximum(size,1e-12)*31).clip(0,31).astype(int)
    occupied=int(len(np.unique(quantized,axis=0)))
    reliable=bool(audit.is_watertight and audit.is_winding_consistent and audit.is_volume)
    if profile['name']=='unknown':checks.append(check('asset_class_known','NeedsReview','No class-specific identity rule selected'))
    return {'status':decision(checks),'checks':checks,'metrics':{'bounds':audit.bounds.tolist(),'extents_x_y_z':size.tolist(),'up_axis':'Y',
        'ratios':ratios,'pca_smallest_largest_variance_ratio':pca_ratio,'surface_area_native_units_squared':total,
        'watertight':bool(audit.is_watertight),'winding_consistent':bool(audit.is_winding_consistent),'volume_reliable':reliable,
        'enclosed_volume_native_units_cubed':float(audit.volume) if reliable else None,'volume_note':'Only reliable consistently oriented watertight meshes expose volume; no human-usable interior volume is inferred.',
        'component_count':len(groups),'significant_component_count':significant,'component_area_fractions_top20':distribution[:20],
        'projected_silhouettes':projections,'surface_shell_occupancy':{'resolution':32,'occupied_cells':occupied,'fraction':occupied/32**3,
        'method':'Sampled vertices and face centroids; occupied surface-shell cells, NOT filled/physical volume.','sample_count':len(points)}}}
