import asyncio
import jupedsim as jps
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import numpy as np
import os
import shapely
import pathlib
import math

app = FastAPI(title="Custom JuPedSim Web Simulator")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class Point(BaseModel):
    x: float
    y: float

class Element(BaseModel):
    type: str
    points: List[Point]
    color: Optional[str] = None
    agentCount: Optional[int] = 10
    crowdComposition: Optional[Dict[str, float]] = None # Local composition
    scenarioType: Optional[str] = None

class SimulationConfig(BaseModel):
    elements: List[Element]
    fps: int = 20
    duration: int = 60 
    ambientTemperature: float = 20.0
    crowdComposition: Dict[str, float] = {"male": 40, "female": 40, "child": 20}
    oceanComposition: Dict[str, float] = {"openness": 20, "conscientiousness": 20, "extraversion": 20, "agreeableness": 20, "neuroticism": 20}
    loiterMode: bool = False
    emergencyMode: bool = False
    emergencyTriggerTime: int = 30

def build_jps_simulation(config: SimulationConfig, trajectory_path: str):
    print(f"DEBUG: Received config with loiterMode={config.loiterMode}, emergencyMode={config.emergencyMode}")
    print(f"DEBUG: Elements received: {[el.type for el in config.elements]}")
    # Separate elements by type
    walkable_polys = []
    obstacle_polys = []
    smoke_obstacle_polys = []
    fire_obstacle_polys = []
    starts = []
    exits = []
    journey_lines = []
    smoke_sources = []
    fire_sources = []
    pois = []
    
    # Pre-parse elements into shapely objects
    for i, el in enumerate(config.elements):
        if el.type == 'scenario':
            if el.scenarioType == 'Smoke':
                sx, sy = el.points[0].x, el.points[0].y
                smoke_sources.append((sx, sy))
                # Add small impassable obstacle to force A* pathfinding around smoke
                smoke_obstacle_polys.append(shapely.Point(sx, sy).buffer(1.0))
            elif el.scenarioType == 'Fire':
                fx, fy = el.points[0].x, el.points[0].y
                fire_sources.append((fx, fy))
                # Add impassable obstacle (reduced to 1.5m radius to avoid completely severing corridors)
                fire_obstacle_polys.append(shapely.Point(fx, fy).buffer(1.5))
            continue
        elif el.type == 'poi':
            pois.append((el.points[0].x, el.points[0].y))
            continue
            
        if len(el.points) < 2: continue
        pts = [(p.x, p.y) for p in el.points]
        
        if el.type == 'journey':
            journey_lines.append({'points': pts, 'id': i})
            continue
            
        if len(el.points) < 3: continue
        try:
            poly = shapely.Polygon(pts)
            if not poly.is_valid: poly = poly.buffer(0)
            
            data = {'poly': poly, 'id': i, 'agentCount': el.agentCount, 'comp': el.crowdComposition}
            if el.type == 'boundary': walkable_polys.append(poly)
            elif el.type == 'obstacle': obstacle_polys.append(poly)
            elif el.type == 'exit': exits.append(data)
            elif el.type == 'start': starts.append(data)
        except: continue

    # Build Simulation Geometry
    if not walkable_polys:
        walkable_polys = [shapely.box(0, 0, 20, 20)]
    area_union = shapely.unary_union(walkable_polys)
    
    all_hard_obstacles = obstacle_polys + fire_obstacle_polys + smoke_obstacle_polys
    if all_hard_obstacles:
        area_union_full = area_union.difference(shapely.unary_union(all_hard_obstacles))
    else:
        area_union_full = area_union
    
    try:
        simulation = jps.Simulation(
            model=jps.SocialForceModel(),
            geometry=shapely.GeometryCollection(area_union_full).wkt,
            trajectory_writer=jps.SqliteTrajectoryWriter(output_file=pathlib.Path(trajectory_path)),
        )
    except RuntimeError as e:
        if "not connected" in str(e).lower() or "accessible area" in str(e).lower():
            print(f"WARNING: Smoke obstacles disconnected the navigation mesh! Falling back to soft-smoke...")
            essential_obstacles = obstacle_polys + fire_obstacle_polys
            if essential_obstacles:
                area_union_essential = area_union.difference(shapely.unary_union(essential_obstacles))
            else:
                area_union_essential = area_union
                
            try:
                simulation = jps.Simulation(
                    model=jps.SocialForceModel(),
                    geometry=shapely.GeometryCollection(area_union_essential).wkt,
                    trajectory_writer=jps.SqliteTrajectoryWriter(output_file=pathlib.Path(trajectory_path)),
                )
            except RuntimeError as e2:
                if "not connected" in str(e2).lower() or "accessible area" in str(e2).lower():
                    error_msg = "NO POSSIBLE PATH: The Fire obstacles completely block the path to the exit. Simulation aborted."
                    print(f"ERROR: {error_msg}")
                    raise ValueError(error_msg)
                else:
                    raise e2
        else:
            raise e
            
    simulation.set_ambient_temperature(config.ambientTemperature)
    
    # Smoke sources are handled dynamically in the sim loop
    
    # Map Exits to JPS Stage IDs
    exit_to_stage = {}
    exit_centroids = {}
    for exit_data in exits:
        stage_id = simulation.add_exit_stage(exit_data['poly'])
        exit_to_stage[exit_data['id']] = stage_id
        exit_centroids[stage_id] = (exit_data['poly'].centroid.x, exit_data['poly'].centroid.y)
        
    # Add POI stages
    poi_stages = []
    for px, py in pois:
        poi_stages.append(simulation.add_waypoint_stage((px, py), 1.0))
    
    # Define Journeys based on Journey elements
    # A journey connects a start to an exit if the journey line points are inside them
    start_to_journey = {} # start_element_id -> journey_id
    
    for jl in journey_lines:
        p_start = shapely.Point(jl['points'][0])
        p_end = shapely.Point(jl['points'][-1])
        
        target_exit_stage = None
        source_start_id = None
        
        # Find which start is the source
        for s in starts:
            if s['poly'].contains(p_start):
                source_start_id = s['id']
                break
        
        # Find which exit is the destination
        for e in exits:
            if e['poly'].contains(p_end):
                target_exit_stage = exit_to_stage[e['id']]
                break
        
        if source_start_id is not None and target_exit_stage is not None:
            # Create a JPS journey for this specific connection
            j_desc = jps.JourneyDescription([target_exit_stage])
            j_id = simulation.add_journey(j_desc)
            start_to_journey[source_start_id] = (j_id, target_exit_stage)

    # Global OCEAN probabilities
    ocean_comp = config.oceanComposition
    ocean_traits = ['openness', 'conscientiousness', 'extraversion', 'agreeableness', 'neuroticism']
    ocean_probs = [ocean_comp.get(t, 20)/100 for t in ocean_traits]
    total_ocean_prob = sum(ocean_probs)
    if total_ocean_prob > 0:
        ocean_probs = [p/total_ocean_prob for p in ocean_probs]
    else:
        ocean_probs = [0.2] * 5

    # Spawn agents and record types
    total_agents = 0
    agent_metadata = {}
    types = ['male', 'female', 'child']
    for s in starts:
        try:
            count = s['agentCount'] or 10
            
            # Use local composition if available, else global
            local_comp = s['comp'] or config.crowdComposition
            local_probs = [local_comp.get('male', 40)/100, local_comp.get('female', 40)/100, local_comp.get('child', 20)/100]
            total_local = sum(local_probs)
            if total_local > 0: local_probs = [p/total_local for p in local_probs]
            else: local_probs = [0.4, 0.4, 0.2]

            positions = jps.distribute_by_number(
                polygon=s['poly'],
                number_of_agents=count,
                distance_to_agents=0.4,
                distance_to_polygon=0.2
            )
            
            journey_info = start_to_journey.get(s['id'])
            
            for pos in positions:
                # Randomly choose type
                a_type = np.random.choice(types, p=local_probs)
                radius = 0.15 if a_type == 'child' else 0.18 # slightly larger for adults
                
                # Random OCEAN trait dominance
                dominant_trait = np.random.choice(ocean_traits, p=ocean_probs)
                
                ocean_values = {t: np.random.uniform(0.0, 0.5) for t in ocean_traits}
                ocean_values[dominant_trait] = np.random.uniform(0.7, 1.0) # Dominant trait is high
                
                params = jps.SocialForceModelAgentParameters(
                    position=pos, 
                    radius=radius,
                    desired_speed=np.random.uniform(0.8, 1.2)
                )
                
                # Bind generic attributes required by the backend
                params.type = a_type
                params.heartbeat = np.random.uniform(60, 80)
                params.stress = np.random.uniform(0.0, 0.2)
                params.panic = 0.0
                for k, v in ocean_values.items():
                    setattr(params, k, v)
                
                if config.loiterMode and poi_stages:
                    # Create a continuous looping journey through POIs
                    poi_order = poi_stages.copy()
                    np.random.shuffle(poi_order)
                    j_desc = jps.JourneyDescription(poi_order)
                    
                    # JuPedSim requires EXPLICIT transitions between stages.
                    # Passing a list to JourneyDescription only registers them, but gives them a 'None' transition.
                    for idx in range(len(poi_order)):
                        next_stage = poi_order[(idx + 1) % len(poi_order)]
                        transition = jps.Transition.create_fixed_transition(next_stage)
                        j_desc.set_transition_for_stage(poi_order[idx], transition)
                        
                    params.journey_id = simulation.add_journey(j_desc)
                    params.stage_id = poi_order[0]
                    print(f"Assigned POI circular journey with {len(poi_order)} waypoints")
                elif journey_info:
                    params.journey_id = journey_info[0]
                    params.stage_id = journey_info[1]
                elif exit_to_stage:
                    first_exit_id = list(exit_to_stage.values())[0]
                    j_fallback = jps.JourneyDescription([first_exit_id])
                    params.journey_id = simulation.add_journey(j_fallback)
                    params.stage_id = first_exit_id
                
                agent_id = simulation.add_agent(params)
                agent_metadata[agent_id] = {
                    "start_id": s['id'],
                    "dominant_trait": dominant_trait,
                    "stress": params.stress,
                    "panic": params.panic,
                    "heartbeat": params.heartbeat,
                    "base_speed": params.desired_speed,
                    "stage_id": getattr(params, 'stage_id', None),
                    "openness": ocean_values["openness"],
                    "conscientiousness": ocean_values["conscientiousness"],
                    "extraversion": ocean_values["extraversion"],
                    "agreeableness": ocean_values["agreeableness"],
                    "neuroticism": ocean_values["neuroticism"]
                }
                total_agents += 1
        except Exception as e:
            print(f"Error spawning in start {s['id']}: {e}")
            
    return simulation, total_agents, agent_metadata, exit_to_stage, poi_stages, fire_sources, smoke_sources, exit_centroids

@app.websocket("/ws/simulation")
async def simulation_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket Accepted")
    recording = None
    fatigue_cache = {} # frame_idx -> {agent_id: fatigue}
    agent_info_cache = {"types": {}} # Map agent_id -> type, persistent across actions
    try:
        while True:
            raw_data = await websocket.receive_json()
            action = raw_data.get("action")
            if action == "calculate":
                config = SimulationConfig(**raw_data["config"])
                traj_file = "simulation_output.sqlite"
                if os.path.exists(traj_file):
                    try: os.remove(traj_file)
                    except: pass
                    
                sim, initial_agents, agent_metadata, exit_to_stage, poi_stages, fire_sources, smoke_sources, exit_centroids = build_jps_simulation(config, traj_file)
                total_steps = config.duration * config.fps
                
                # Cache types and metadata for playback
                agent_info_cache["types"] = {} 
                agent_info_cache["metadata"] = agent_metadata
                
                # JuPedSim often records at a lower frequency than calculation.
                # Based on the logs, there's a 4:1 ratio.
                sampling_rate = 4 
                
                heatmaps_cache = {} # Map frame_idx -> heatmap
                fatigue_cache = {} # Map frame_idx -> {agent_id: fatigue}
                print(f"Starting calculation: {total_steps} steps, {initial_agents} agents")
                
                emergency_trigger_step = config.emergencyTriggerTime * config.fps if config.emergencyMode else -1
                emergency_triggered = False
                
                for step in range(total_steps):
                    # Hazard Avoidance: Smoke causes 50% speed reduction
                    if smoke_sources:
                        for a in sim.agents():
                            min_dist = min([math.hypot(a.position[0]-sx, a.position[1]-sy) for sx, sy in smoke_sources])
                            base_speed = agent_metadata[a.id]["base_speed"]
                            if min_dist < 5.0:
                                a.model.desired_speed = base_speed * 0.5
                            else:
                                a.model.desired_speed = base_speed

                    if config.emergencyMode and not emergency_triggered and step >= emergency_trigger_step:
                        emergency_triggered = True
                        if exit_centroids:
                            em_journeys = {}
                            for stage_id in exit_centroids:
                                em_journeys[stage_id] = sim.add_journey(jps.JourneyDescription([stage_id]))
                            
                            for a in sim.agents():
                                best_stage = None
                                min_cost = float('inf')
                                for stage_id, (cx, cy) in exit_centroids.items():
                                    dist_to_exit = math.hypot(a.position[0]-cx, a.position[1]-cy)
                                    min_f_dist = min([math.hypot(cx-fx, cy-fy) for fx, fy in fire_sources]) if fire_sources else float('inf')
                                    min_s_dist = min([math.hypot(cx-sx, cy-sy) for sx, sy in smoke_sources]) if smoke_sources else float('inf')
                                    
                                    fire_pen = 1000.0 / max(min_f_dist, 1.0) if fire_sources else 0.0
                                    smoke_pen = 500.0 / max(min_s_dist, 1.0) if smoke_sources else 0.0
                                    cost = dist_to_exit + fire_pen + smoke_pen
                                    
                                    if cost < min_cost:
                                        min_cost = cost
                                        best_stage = stage_id
                                        
                                if best_stage:
                                    sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[best_stage], stage_id=best_stage)
                                
                    if step > 0 and sim.agent_count() == 0:
                        print(f"Early exit: Step {step}")
                        break
                    sim.iterate()
                    if step % 100 == 0:
                        print(f"Step {step}/{total_steps}... (Agents: {sim.agent_count()})")
                    if True: # Send every step for high-resolution trails
                        agent_data = []
                        for a in sim.agents():
                            fatigue_val = getattr(a.model, 'fatigue', 0.0)
                            a_type = getattr(a, 'type', 'male')
                            agent_data.append({
                                "id": a.id, "x": a.position[0], "y": a.position[1],
                                "target_x": a.target[0] if a.target else a.position[0],
                                "target_y": a.target[1] if a.target else a.position[1],
                                "fatigue": fatigue_val,
                                "type": a_type
                            })
                            
                            # Cache type for playback
                            agent_info_cache["types"][a.id] = a_type
                            
                            # Add metadata
                            m = agent_metadata.get(a.id, {})
                            agent_data[-1].update(m)
                            
                            if step % sampling_rate == 0:
                                frame_idx = step // sampling_rate
                                if frame_idx not in fatigue_cache: fatigue_cache[frame_idx] = {}
                                fatigue_cache[frame_idx][a.id] = fatigue_val
                        
                        heatmap_data = sim.get_heatmap()
                        if step % sampling_rate == 0:
                            frame_idx = step // sampling_rate
                            heatmap_data["frame_idx"] = frame_idx
                            heatmaps_cache[frame_idx] = heatmap_data
                            
                        await websocket.send_json({
                            "type": "progress", 
                            "percent": int((step / total_steps) * 100), 
                            "agents": agent_data,
                            "heatmap": heatmap_data # Send every step for perfect sync
                        })
                
                if hasattr(sim, '_writer'): sim._writer.close()
                await websocket.send_json({"type": "finished", "file": traj_file})
                
            elif action == "load_recording":
                traj_file = raw_data.get("file", "simulation_output.sqlite")
                if os.path.exists(traj_file):
                    recording = jps.Recording(traj_file)
                    await websocket.send_json({"type": "recording_info", "num_frames": recording.num_frames, "fps": recording.fps})
            elif action == "get_frame":
                frame_idx = raw_data.get("frame", 0)
                if recording and frame_idx < recording.num_frames:
                    frame = recording.frame(frame_idx)
                    agents = []
                    for a in frame.agents:
                        f_val = fatigue_cache[frame_idx].get(a.id, 0.0) if frame_idx < len(fatigue_cache) else 0.0
                        a_type = agent_info_cache["types"].get(a.id, 'male')
                        agents.append({
                            "id": a.id, "x": a.position[0], "y": a.position[1], 
                            "fatigue": f_val,
                            "type": a_type
                        })
                        # Add metadata for playback
                        m = agent_info_cache.get("metadata", {}).get(a.id, {})
                        agents[-1].update(m)
                    
                    hm = heatmaps_cache[frame_idx] if frame_idx < len(heatmaps_cache) else None
                    if hm:
                        hm["frame_idx"] = frame_idx
                    
                    await websocket.send_json({
                        "type": "frame_data", 
                        "frame": frame_idx, 
                        "agents": agents,
                        "heatmap": hm
                    })
            elif action == "stop": break
    except Exception as e:
        import traceback
        traceback.print_exc()
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
