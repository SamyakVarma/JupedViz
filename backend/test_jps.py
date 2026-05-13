import jupedsim as jps
import numpy as np

def test():
    # 1. Geometry
    geo = jps.Geometry()
    # A simple 10x10 square room
    poly = [(0,0), (10,0), (10,10), (0,10)]
    geo.add_walkable_area(poly)
    
    # 2. Model
    model = jps.CollisionFreeSpeedModel()
    
    # 3. Simulation
    sim = jps.Simulation(model=model, geometry=geo, dt=0.05)
    
    # 4. Add Agents
    params = jps.CollisionFreeSpeedModelAgentParameters()
    for i in range(5):
        sim.add_agent(jps.Agent(pos=(1+i, 1+i), parameters=params))
        
    # 5. Iterate
    for i in range(10):
        sim.iterate()
        print(f"Step {i}: Agent 0 at {sim.agent(0).pos}")

if __name__ == "__main__":
    test()
