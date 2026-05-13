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

class SimulationConfig(BaseModel):
    elements: List[Element]
    fps: int = 20
    duration: int = 60 
    ambientTemperature: float = 20.0

def build_jps_simulation(config: SimulationConfig, trajectory_path: str):
    # Separate elements by type
    walkable_polys = []
    obstacle_polys = []
    starts = []
    exits = []
    journey_lines = []
    
    # Pre-parse elements into shapely objects
    for i, el in enumerate(config.elements):
        if len(el.points) < 2: continue
        pts = [(p.x, p.y) for p in el.points]
        
        if el.type == 'journey':
            journey_lines.append({'points': pts, 'id': i})
            continue
            
        if len(el.points) < 3: continue
        try:
            poly = shapely.Polygon(pts)
            if not poly.is_valid: poly = poly.buffer(0)
            
            data = {'poly': poly, 'id': i, 'agentCount': el.agentCount}
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

    # Distribute Agents
    total_agents = 0
    for s in starts:
        try:
            count = s['agentCount'] or 10
            positions = jps.distribute_by_number(
                polygon=s['poly'],
                number_of_agents=count,
                distance_to_agents=0.4,
                distance_to_polygon=0.2
            )
            
            # Check if this start has an assigned journey
            journey_info = start_to_journey.get(s['id'])
            
            for pos in positions:
                params = jps.CollisionFreeSpeedModelV2AgentParameters(position=pos, radius=0.15)
                if journey_info:
                    params.journey_id = journey_info[0]
                    params.stage_id = journey_info[1]
                elif exit_to_stage:
                    # Fallback to first available exit if no journey defined
                    first_exit_id = list(exit_to_stage.values())[0]
                    j_fallback = jps.JourneyDescription([first_exit_id])
                    params.journey_id = simulation.add_journey(j_fallback)
                    params.stage_id = first_exit_id
                
                simulation.add_agent(params)
                total_agents += 1
        except Exception as e:
            print(f"Error spawning in start {s['id']}: {e}")
            
    return simulation, total_agents

@app.websocket("/ws/simulation")
async def simulation_stream(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket Accepted")
    recording = None
    heatmaps_cache = []
    fatigue_cache = {} # frame_idx -> {agent_id: fatigue}
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
                    
                sim, initial_agents = build_jps_simulation(config, traj_file)
                total_steps = config.duration * config.fps
                
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
                            agent_data.append({
                                "id": a.id, "x": a.position[0], "y": a.position[1],
                                "target_x": a.target[0] if a.target else a.position[0],
                                "target_y": a.target[1] if a.target else a.position[1],
                                "fatigue": fatigue_val
                            })
                            
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
                        agents.append({"id": a.id, "x": a.position[0], "y": a.position[1], "fatigue": f_val})
                    
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
        await websocket.close()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
