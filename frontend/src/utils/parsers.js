/**
 * Simple WKT (Well-Known Text) Parser for JuPedSim shapes
 */
export const parseWKT = (wkt) => {
  const typeMatch = wkt.match(/^([A-Z]+)/);
  if (!typeMatch) return null;

  const type = typeMatch[1];
  const coordinatesStr = wkt.match(/\(\(?(.*?)\)?\)\)?/);
  if (!coordinatesStr) return null;

  const pairs = coordinatesStr[1].split(',').map(pair => {
    const [x, y] = pair.trim().split(/\s+/).map(Number);
    return { x, y };
  });

  return {
    type: type.toLowerCase(),
    points: pairs
  };
};

export const parseJSONWKT = (jsonStr) => {
  try {
    const data = JSON.parse(jsonStr);
    if (Array.isArray(data)) {
      return data.map(item => parseWKT(item.wkt || item.geometry || item));
    } else if (data.wkt) {
      return [parseWKT(data.wkt)];
    }
    return null;
  } catch (e) {
    console.error("Failed to parse JSON+WKT", e);
    return null;
  }
};

/**
 * Basic DXF Parser (supports LINE and LWPOLYLINE with Layer mapping)
 */
export const parseDXF = (dxfContent) => {
  const lines = dxfContent.split(/\r?\n/);
  const entities = [];
  let currentEntity = null;

  for (let i = 0; i < lines.length; i++) {
    const groupCode = lines[i].trim();
    const value = lines[i + 1]?.trim();
    i++;

    if (groupCode === '0') {
      if (currentEntity) entities.push(currentEntity);
      if (value === 'LINE' || value === 'LWPOLYLINE') {
        currentEntity = { type: 'polyline', points: [], layer: '0' };
      } else {
        currentEntity = null;
      }
    } else if (currentEntity) {
      // Layer Name
      if (groupCode === '8') {
        currentEntity.layer = value.toLowerCase();
        // Map common JuPedSim layer names to types
        if (currentEntity.layer.includes('walkablearea')) {
          currentEntity.type = 'boundary';
        } else if (currentEntity.layer.includes('exit')) {
          currentEntity.type = 'exit';
        } else if (currentEntity.layer.includes('obstacle')) {
          currentEntity.type = 'obstacle';
        } else if (currentEntity.layer.includes('entry') || currentEntity.layer.includes('start') || currentEntity.layer.includes('distribution')) {
          currentEntity.type = 'start';
        } else if (currentEntity.layer.includes('journey') || currentEntity.layer.includes('routing')) {
          currentEntity.type = 'journey';
          // Assign a random color if not present
          currentEntity.color = `hsl(${Math.random() * 360}, 70%, 60%)`;
        } else if (currentEntity.layer.includes('waypoint')) {
          currentEntity.type = 'auxiliary';
        }
      }

      if (value === 'LINE' || currentEntity.type === 'line') { // Handle cases where type might be changed
         // Actually currentEntity.type is initialized as 'polyline' or assigned from layer.
         // Let's keep logic simple.
      }

      // Coordinates
      if (groupCode === '10') {
        if (currentEntity.points.length === 0 || currentEntity.points[currentEntity.points.length - 1].y !== undefined) {
          currentEntity.points.push({ x: Number(value) });
        } else {
          currentEntity.points[currentEntity.points.length - 1].x = Number(value);
        }
      } else if (groupCode === '20') {
        if (currentEntity.points.length > 0) {
          currentEntity.points[currentEntity.points.length - 1].y = Number(value);
        }
      } else if (groupCode === '11') {
        currentEntity.endX = Number(value);
      } else if (groupCode === '21') {
        currentEntity.endY = Number(value);
      }
    }
  }

  return entities.map(e => {
    if (e.endX !== undefined && e.endY !== undefined) {
      e.points.push({ x: e.endX, y: e.endY });
    }
    return e;
  });
};

/**
 * Basic IFC Parser (Extracts PolyLines and CartesianPoints)
 */
export const parseIFC = (ifcContent) => {
  const points = new Map();
  const shapes = [];

  // 1. Extract Points
  const pointMatches = ifcContent.matchAll(/#(\d+)\s*=\s*IFCCARTESIANPOINT\s*\(\((.*?)\)\)/g);
  for (const match of pointMatches) {
    const id = match[1];
    const coords = match[2].split(',').map(c => parseFloat(c.replace(/\.$/, '0')));
    points.set(id, { x: coords[0] || 0, y: coords[1] || 0 });
  }

  // 2. Extract Polylines
  const polylineMatches = ifcContent.matchAll(/#(\d+)\s*=\s*IFCPOLYLINE\s*\(\((.*?)\)\)/g);
  for (const match of polylineMatches) {
    const refIds = match[2].split(',').map(r => r.trim().replace('#', ''));
    const polyPoints = refIds.map(rid => points.get(rid)).filter(Boolean);
    if (polyPoints.length > 0) {
      shapes.push({ type: 'polyline', points: polyPoints });
    }
  }

  return shapes;
};
