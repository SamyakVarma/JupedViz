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
        
    # Panic Journeys (pointing to entry ways)
    panic_journeys = {}
    for s in starts:
        cx, cy = s['poly'].centroid.x, s['poly'].centroid.y
        stage_id = simulation.add_waypoint_stage((cx, cy), 1.0)
        j_id = simulation.add_journey(jps.JourneyDescription([stage_id]))
        panic_journeys[s['id']] = (j_id, stage_id)
    
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
                params.heartbeat = np.random.uniform(70, 90)
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
                    "base_heartbeat": params.heartbeat,
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
            
    return simulation, total_agents, agent_metadata, exit_to_stage, poi_stages, fire_sources, smoke_sources, exit_centroids, panic_journeys

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
                    
                sim, initial_agents, agent_metadata, exit_to_stage, poi_stages, fire_sources, smoke_sources, exit_centroids, panic_journeys = build_jps_simulation(config, traj_file)
                total_steps = config.duration * config.fps
                
                # Cache types and metadata for playback
                agent_info_cache["types"] = {} 
                agent_info_cache["metadata"] = agent_metadata
                
                # JuPedSim often records at a lower frequency than calculation.
                # Based on the logs, there's a 4:1 ratio.
                sampling_rate = 4 
                
                heatmaps_cache = {} # Map frame_idx -> heatmap
                fatigue_cache = {} # Map frame_idx -> {agent_id: fatigue}
                panic_cache = {} # Map frame_idx -> {agent_id: panic}
                events_cache = {} # Map frame_idx -> event_data
                print(f"Starting calculation: {total_steps} steps, {initial_agents} agents")
                
                emergency_trigger_step = config.emergencyTriggerTime * config.fps if config.emergencyMode else -1
                emergency_triggered = False
                clearance_triggered = False  # Intermediate mode: agents spread out
                em_journeys = {}
                
                # Casualty tracking
                casualties = 0
                casualty_log = []  # List of {step, agent_id, cause}
                exited_agents = set()  # Track agents that left via exits (not casualties)
                
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

                    # === PANIC ACCUMULATION (runs every frame, all modes) ===
                    import random
                    from collections import Counter
                    
                    stampede_alert = None  # Will be set if auto-stampede triggers this frame
                    clearance_alert = None  # Will be set if clearance mode triggers this frame
                    clearance_ended = False  # True when clearance mode successfully resolves
                    
                    if fire_sources or smoke_sources:
                        for a in sim.agents():
                            m = agent_metadata[a.id]
                            
                            # Panic Accumulation (fire + smoke)
                            # Panic Accumulation with 10m cutoff
                            fire_dist = min([math.hypot(a.position[0]-fx, a.position[1]-fy) for fx, fy in fire_sources]) if fire_sources else 10.0
                            smoke_dist = min([math.hypot(a.position[0]-sx, a.position[1]-sy) for sx, sy in smoke_sources]) if smoke_sources else 10.0
                            panic_rate = ((0.015 / max(min(fire_dist, 10.0), 1.0)) if fire_sources and fire_dist < 10.0 else 0.0) + \
                                        ((0.02 / max(min(smoke_dist, 10.0), 1.0)) if smoke_sources and smoke_dist < 10.0 else 0.0)
                            neuroticism = m.get("neuroticism", 0.5)
                            
                            # Crowd density amplifier
                            nearby_count = len(sim.agents_in_range(a.position, 2.0)) - 1
                            density_factor = 1.0 + 0.25 * max(0, nearby_count)
                            
                            m["panic"] = min(1.0, m["panic"] + panic_rate * (1.0 + neuroticism) * density_factor)
                            
                            # Derive heartbeat and stress from panic
                            p = m["panic"]
                            base_hr = m.get("base_heartbeat", 70.0)
                            m["heartbeat"] = base_hr + 40.0 * p  # 70-90 calm → 110-130 full panic
                            m["stress"] = min(1.0, p * 1.2 + neuroticism * 0.1)
                            
                            # Continuous Social Force Modulators
                            a.model.desired_speed = m["base_speed"] * (1.0 + 1.5 * p)
                            a.model.mass = 80.0 + 40.0 * p
                            a.model.reaction_time = 0.5 - 0.4 * p
                            a.model.force_distance = 0.08 - 0.06 * p
                    
                    # Crowd-density-only panic (works even without fire/smoke)
                    for a in sim.agents():
                        m = agent_metadata[a.id]
                        nearby_count = len(sim.agents_in_range(a.position, 1.5)) - 1
                        # Threshold depends on traits: extraverts + agreeable tolerate crowds, neurotic introverts don't
                        extraversion = m.get("extraversion", 0.5)
                        agreeableness = m.get("agreeableness", 0.5)
                        neuroticism = m.get("neuroticism", 0.5)
                        # Threshold: 2 (neurotic introvert) to 7 (agreeable extravert)
                        crowd_tolerance = 2 + int(3.0 * extraversion + 2.0 * agreeableness)
                        if nearby_count >= crowd_tolerance:
                            excess = nearby_count - crowd_tolerance + 1
                            density_panic = 0.001 * excess * (1.0 + neuroticism) * (1.0 - 0.3 * extraversion)
                            m["panic"] = min(1.0, m.get("panic", 0.0) + density_panic)
                            # Update derived values
                            p = m["panic"]
                            base_hr = m.get("base_heartbeat", 70.0)
                            m["heartbeat"] = base_hr + 90.0 * p
                            m["stress"] = min(1.0, p * 1.2 + neuroticism * 0.1)
                    
                    # === CLEARANCE / STAMPEDE STATE MACHINE (requires 15+ agents) ===
                    if sim.agent_count() >= 15:
                        avg_panic = sum(agent_metadata[a.id].get("panic", 0.0) for a in sim.agents()) / sim.agent_count()
                        
                        if not clearance_triggered and not emergency_triggered and avg_panic > 0.3:
                            # ENTER CLEARANCE MODE: agents spread out
                            clearance_triggered = True
                            clearance_alert = {"message": "CROWD CLEARANCE — Agents dispersing", "step": step}
                            print(f"\n  ⚠ CLEARANCE MODE at step {step}! Average panic: {avg_panic:.2f}\n")
                        
                        elif clearance_triggered and not emergency_triggered:
                            # CLEARANCE MODE ACTIVE: check for escalation or resolution
                            if avg_panic > 0.65:
                                # ESCALATE TO STAMPEDE
                                clearance_triggered = False
                                emergency_triggered = True
                                stampede_alert = {"message": "STAMPEDE DETECTED — Emergency Evacuation Triggered", "step": step}
                                print(f"\n  ⚠⚠⚠ AUTO-STAMPEDE at step {step}! Average panic: {avg_panic:.2f} ⚠⚠⚠\n")
                                if exit_centroids:
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
                                            agent_metadata[a.id]["safe_exit_stage"] = best_stage
                                            sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[best_stage], stage_id=best_stage)
                                        elif poi_stages:
                                            poi_order = poi_stages.copy()
                                            j_desc = jps.JourneyDescription(poi_order)
                                            for idx in range(len(poi_order)):
                                                next_stage = poi_order[(idx + 1) % len(poi_order)]
                                                j_desc.set_transition_for_stage(poi_order[idx], jps.Transition.create_fixed_transition(next_stage))
                                            fallback_j = sim.add_journey(j_desc)
                                            sim.switch_agent_journey(agent_id=a.id, journey_id=fallback_j, stage_id=poi_order[0])
                                            agent_metadata[a.id]["no_exit"] = True
                            
                            elif avg_panic < 0.15:
                                # CLEARANCE SUCCESSFUL: panic subsided, return to normal
                                clearance_triggered = False
                                clearance_ended = True
                                print(f"\n  ✓ CLEARANCE RESOLVED at step {step}! Average panic: {avg_panic:.2f}\n")
                    
                    # === CLEARANCE MODE BEHAVIOR ===
                    if clearance_triggered:
                        for a in sim.agents():
                            m = agent_metadata[a.id]
                            p = m.get("panic", 0.0)
                            # Increase personal space dramatically (3x normal)
                            a.model.force_distance = 0.25
                            # Slightly faster movement to spread out
                            a.model.desired_speed = m["base_speed"] * 1.2
                            # More responsive to avoid others
                            a.model.reaction_time = 0.3
                            # Enhanced panic decay during clearance (5x normal)
                            m["panic"] = max(0.0, p - 0.003)
                    
                    # === MANUAL EMERGENCY TRIGGER ===
                    if config.emergencyMode and step >= emergency_trigger_step and not emergency_triggered:
                        emergency_triggered = True
                        stampede_alert = {"message": "STAMPEDE DETECTED — Emergency Evacuation Triggered", "step": step}
                        if exit_centroids:
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
                                    agent_metadata[a.id]["safe_exit_stage"] = best_stage
                                    sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[best_stage], stage_id=best_stage)
                                elif poi_stages:
                                    poi_order = poi_stages.copy()
                                    j_desc = jps.JourneyDescription(poi_order)
                                    for idx in range(len(poi_order)):
                                        next_stage = poi_order[(idx + 1) % len(poi_order)]
                                        j_desc.set_transition_for_stage(poi_order[idx], jps.Transition.create_fixed_transition(next_stage))
                                    fallback_j = sim.add_journey(j_desc)
                                    sim.switch_agent_journey(agent_id=a.id, journey_id=fallback_j, stage_id=poi_order[0])
                                    agent_metadata[a.id]["no_exit"] = True
                    
                    # === PROGRESSIVE EXIT RE-EVALUATION (only after emergency triggered) ===
                    if emergency_triggered and em_journeys:
                        def get_visible_exits(agent, panic_level, all_exits):
                            fov_half = math.pi - (5.0 * math.pi / 6.0) * panic_level
                            max_dist = 50.0 - 40.0 * panic_level
                            
                            vx, vy = agent.model.velocity
                            has_heading = (abs(vx) + abs(vy)) > 0.01
                            facing = math.atan2(vy, vx) if has_heading else None
                            
                            visible = {}
                            for stage_id, (cx, cy) in all_exits.items():
                                dx, dy = cx - agent.position[0], cy - agent.position[1]
                                dist = math.hypot(dx, dy)
                                if dist > max_dist:
                                    continue
                                if facing is not None and panic_level > 0.2:
                                    angle_to_exit = math.atan2(dy, dx)
                                    delta = abs(angle_to_exit - facing)
                                    delta = min(delta, 2.0 * math.pi - delta)
                                    if delta > fov_half:
                                        continue
                                visible[stage_id] = (cx, cy)
                            return visible
                        
                        for a in sim.agents():
                            m = agent_metadata[a.id]
                            p = m.get("panic", 0.0)
                            
                            if p > 0.8:
                                m["is_panicking"] = True
                                if step % 10 == 0:
                                    neighbor_journeys = []
                                    for other_a in sim.agents():
                                        if other_a.id != a.id:
                                            dist = math.hypot(a.position[0]-other_a.position[0], a.position[1]-other_a.position[1])
                                            if dist < 3.0:
                                                neighbor_journeys.append(other_a.journey_id)
                                    if neighbor_journeys:
                                        most_common = Counter(neighbor_journeys).most_common(1)[0][0]
                                        if most_common != a.journey_id:
                                            journey_to_stage = {v: k for k, v in em_journeys.items()}
                                            if most_common in journey_to_stage:
                                                sim.switch_agent_journey(agent_id=a.id, journey_id=most_common, stage_id=journey_to_stage[most_common])
                                                m["safe_exit_stage"] = journey_to_stage[most_common]
                                    else:
                                        visible = get_visible_exits(a, p, exit_centroids)
                                        if visible:
                                            nearest = min(visible.keys(), key=lambda s: math.hypot(a.position[0]-visible[s][0], a.position[1]-visible[s][1]))
                                            if nearest != m.get("safe_exit_stage") and nearest in em_journeys:
                                                sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[nearest], stage_id=nearest)
                                                m["safe_exit_stage"] = nearest
                                                
                            elif p > 0.6:
                                m["is_panicking"] = True
                                if step % 40 == 0:
                                    visible = get_visible_exits(a, p, exit_centroids)
                                    if visible:
                                        sorted_exits = sorted(visible.keys(), key=lambda s: math.hypot(a.position[0]-visible[s][0], a.position[1]-visible[s][1]))
                                        candidates = {s: visible[s] for s in sorted_exits[:2]}
                                        
                                        best_stage = None
                                        min_cost = float('inf')
                                        for stage_id, (cx, cy) in candidates.items():
                                            dist_to_exit = math.hypot(a.position[0]-cx, a.position[1]-cy)
                                            min_f_dist = min([math.hypot(cx-fx, cy-fy) for fx, fy in fire_sources]) if fire_sources else float('inf')
                                            fire_pen = 200.0 / max(min_f_dist, 1.0) if fire_sources else 0.0
                                            cost = dist_to_exit + fire_pen
                                            if cost < min_cost:
                                                min_cost = cost
                                                best_stage = stage_id
                                        
                                        if best_stage and best_stage != m.get("safe_exit_stage") and best_stage in em_journeys:
                                            sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[best_stage], stage_id=best_stage)
                                            m["safe_exit_stage"] = best_stage
                                            
                            elif p > 0.3:
                                if step % 100 == 0:
                                    visible = get_visible_exits(a, p, exit_centroids)
                                    if visible:
                                        best_stage = None
                                        min_cost = float('inf')
                                        for stage_id, (cx, cy) in visible.items():
                                            dist_to_exit = math.hypot(a.position[0]-cx, a.position[1]-cy)
                                            min_f_dist = min([math.hypot(cx-fx, cy-fy) for fx, fy in fire_sources]) if fire_sources else float('inf')
                                            min_s_dist = min([math.hypot(cx-sx, cy-sy) for sx, sy in smoke_sources]) if smoke_sources else float('inf')
                                            fire_pen = 1000.0 / max(min_f_dist, 1.0) if fire_sources else 0.0
                                            smoke_pen = 500.0 / max(min_s_dist, 1.0) if smoke_sources else 0.0
                                            cost = dist_to_exit + fire_pen + smoke_pen
                                            if cost < min_cost:
                                                min_cost = cost
                                                best_stage = stage_id
                                        
                                        if best_stage and best_stage != m.get("safe_exit_stage") and best_stage in em_journeys:
                                            sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[best_stage], stage_id=best_stage)
                                            m["safe_exit_stage"] = best_stage
                                                
                    # Global Panic Decay & Recovery
                    agents_to_trample = []
                    for a in sim.agents():
                        m = agent_metadata[a.id]
                        
                        # Distance to exit decay
                        min_exit_dist = 100.0
                        if exit_centroids:
                            min_exit_dist = min([math.hypot(a.position[0]-cx, a.position[1]-cy) for cx, cy in exit_centroids.values()])
                        
                        decay = 0.005 if min_exit_dist < 5.0 else 0.0005
                        m["panic"] = max(0.0, m.get("panic", 0.0) - decay)
                        
                        # FREEZE RESPONSE: probability-based, driven by neuroticism + panic
                        p = m.get("panic", 0.0)
                        neuroticism = m.get("neuroticism", 0.5)
                        extraversion = m.get("extraversion", 0.5)
                        if not m.get("frozen", False) and p > 0.8:
                            # Freeze probability per frame: high neuroticism + low extraversion = more likely
                            # Base chance ~0.5% per frame, scaled by traits
                            freeze_chance = 0.001 * neuroticism * (1.0 - extraversion)
                            if random.random() < freeze_chance:
                                m["frozen"] = True
                                a.model.desired_speed = 0.0
                        elif m.get("frozen", False) and p < 0.3:
                            m["frozen"] = False
                            a.model.desired_speed = m["base_speed"]
                        elif m.get("frozen", False):
                            a.model.desired_speed = 0.0  # Keep frozen
                        
                        # TRAMPLING: Fast panicked agents trample frozen neighbors
                        if p > 0.6 and a.model.desired_speed > 1.5 and not m.get("frozen", False):
                            nearby_ids = sim.agents_in_range(a.position, 0.35)
                            for nid in nearby_ids:
                                if nid != a.id and nid in agent_metadata:
                                    nm = agent_metadata[nid]
                                    if nm.get("frozen", False) and random.random() < 0.02:  # 2% per frame
                                        agents_to_trample.append(nid)
                        
                        if m.get("is_panicking", False) and m["panic"] < 0.3:
                            m["is_panicking"] = False
                            m["frozen"] = False
                            safe_exit = m.get("safe_exit_stage")
                            if safe_exit and emergency_triggered and safe_exit in em_journeys:
                                sim.switch_agent_journey(agent_id=a.id, journey_id=em_journeys[safe_exit], stage_id=safe_exit)
                    
                    # Process tramplings
                    casualty_event = None
                    for tid in set(agents_to_trample):
                        try:
                            sim.mark_agent_for_removal(tid)
                            casualties += 1
                            casualty_log.append({"step": step, "agent_id": tid, "cause": "trampled"})
                            print(f"  ☠ Agent {tid} TRAMPLED at step {step} (casualties: {casualties})")
                            casualty_event = {"cause": "trampled", "total": casualties}
                        except: pass
                                
                    if step > 0 and sim.agent_count() == 0:
                        print(f"Early exit: Step {step}")
                        break
                    try:
                        sim.iterate()
                    except RuntimeError as e:
                        err_msg = str(e)
                        if "outside of accessible area" in err_msg:
                            import re
                            match = re.search(r"Point \(([^,]+), ([^)]+)\)", err_msg)
                            if match:
                                px, py = float(match.group(1)), float(match.group(2))
                                closest_agent = None
                                min_dist = float('inf')
                                for a in sim.agents():
                                    dist = math.hypot(a.position[0]-px, a.position[1]-py)
                                    if dist < min_dist:
                                        min_dist = dist
                                        closest_agent = a
                                if closest_agent and min_dist < 2.0:
                                    sim.mark_agent_for_removal(closest_agent.id)
                                    casualties += 1
                                    casualty_log.append({"step": step, "agent_id": closest_agent.id, "cause": "crushed against wall"})
                                    print(f"  ☠ Agent {closest_agent.id} CRUSHED at step {step} (casualties: {casualties})")
                                    casualty_event = {"cause": "crushed against wall", "total": casualties}
                        else:
                            raise e
                    
                    # Track agents that exited normally (not casualties)
                    for rid in sim.removed_agents():
                        if rid not in [c["agent_id"] for c in casualty_log]:
                            exited_agents.add(rid)
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
                                if frame_idx not in panic_cache: panic_cache[frame_idx] = {}
                                panic_cache[frame_idx][a.id] = m.get("panic", 0.0)
                        
                        heatmap_data = sim.get_heatmap()
                        if step % sampling_rate == 0:
                            frame_idx = step // sampling_rate
                            heatmap_data["frame_idx"] = frame_idx
                            heatmaps_cache[frame_idx] = heatmap_data
                            
                            if casualty_event or stampede_alert or clearance_alert or clearance_ended:
                                if frame_idx not in events_cache: events_cache[frame_idx] = {}
                                if casualty_event: events_cache[frame_idx]["casualty_event"] = casualty_event
                                if stampede_alert: events_cache[frame_idx]["stampede_alert"] = stampede_alert
                                if clearance_alert: events_cache[frame_idx]["clearance_alert"] = clearance_alert
                                if clearance_ended: events_cache[frame_idx]["clearance_ended"] = True
                            
                        progress_msg = {
                            "type": "progress", 
                            "percent": int((step / total_steps) * 100), 
                            "agents": agent_data,
                            "heatmap": heatmap_data,
                            "casualties": casualties
                        }
                        if casualty_event:
                            progress_msg["casualty_event"] = casualty_event
                            casualty_event = None
                        if stampede_alert:
                            progress_msg["stampede_alert"] = stampede_alert
                        if clearance_alert:
                            progress_msg["clearance_alert"] = clearance_alert
                        if clearance_ended:
                            progress_msg["clearance_ended"] = True
                        await websocket.send_json(progress_msg)
                
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
                        p_val = panic_cache[frame_idx].get(a.id, 0.0) if frame_idx < len(panic_cache) else 0.0
                        a_type = agent_info_cache["types"].get(a.id, 'male')
                        agents.append({
                            "id": a.id, "x": a.position[0], "y": a.position[1], 
                            "fatigue": f_val,
                            "type": a_type,
                            "panic": p_val
                        })
                        # Add metadata for playback
                        m = agent_info_cache.get("metadata", {}).get(a.id, {})
                        
                        # Copy to avoid mutating original, remove panic so it doesn't overwrite dynamic p_val
                        m_copy = m.copy()
                        if "panic" in m_copy: del m_copy["panic"]
                        agents[-1].update(m_copy)
                    
                    hm = heatmaps_cache[frame_idx] if frame_idx < len(heatmaps_cache) else None
                    if hm:
                        hm["frame_idx"] = frame_idx
                    
                    frame_msg = {
                        "type": "frame_data", 
                        "frame": frame_idx, 
                        "agents": agents,
                        "heatmap": hm
                    }
                    if frame_idx in events_cache:
                        frame_msg.update(events_cache[frame_idx])
                        
                    await websocket.send_json(frame_msg)
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
