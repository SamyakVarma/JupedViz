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

def build_jps_simulation(config: SimulationConfig, trajectory_path: str):
    # Separate elements by type
    walkable_polys = []
    obstacle_polys = []
    starts = []
    exits = []
    journey_lines = []
    smoke_sources = []
    
    # Pre-parse elements into shapely objects
    for i, el in enumerate(config.elements):
        if el.type == 'scenario':
            if el.scenarioType == 'Smoke':
                smoke_sources.append((el.points[0].x, el.points[0].y))
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
    if obstacle_polys:
        area_union = area_union.difference(shapely.unary_union(obstacle_polys))
    
    simulation = jps.Simulation(
        model=jps.CollisionFreeSpeedModelV2(),
        geometry=shapely.GeometryCollection(area_union).wkt,
        trajectory_writer=jps.SqliteTrajectoryWriter(output_file=pathlib.Path(trajectory_path)),
    )
    simulation.set_ambient_temperature(config.ambientTemperature)
    
    for sx, sy in smoke_sources:
        simulation.add_smoke_source((sx, sy), 50.0) # Emission rate 50.0
    
    # Map Exits to JPS Stage IDs
    exit_to_stage = {}
    for exit_data in exits:
        stage_id = simulation.add_exit_stage(exit_data['poly'])
        exit_to_stage[exit_data['id']] = stage_id
    
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
                
                params = jps.CollisionFreeSpeedModelV2AgentParameters(
                    position=pos, 
                    radius=radius, 
                    type=a_type,
                    heartbeat=np.random.uniform(60, 80),
                    stress=np.random.uniform(0.0, 0.2),
                    panic=0.0,
                    **ocean_values
                )
                
                if journey_info:
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
            
    return simulation, total_agents, agent_metadata

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
                    
                sim, initial_agents, agent_metadata = build_jps_simulation(config, traj_file)
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
                
                for step in range(total_steps):
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
