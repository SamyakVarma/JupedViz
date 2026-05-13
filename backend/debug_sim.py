import jupedsim as jps

def test():
    poly = [(0,0), (10,0), (10,10), (0,10)]
    geo = jps.geometry_utils.build_geometry(poly)
    wkt = geo.as_wkt()
    print(f"WKT: {wkt}")
    
    model = jps.CollisionFreeSpeedModel()
    try:
        sim = jps.Simulation(model=model, geometry=wkt, dt=0.05)
        print("Simulation created successfully with WKT")
    except Exception as e:
        print(f"Failed with WKT: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test()
