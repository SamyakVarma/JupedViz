import React, { useState, useEffect, useRef } from 'react';
import './App.css';
import {
  Settings, FileText, Box, Share2,
  MousePointer2, Move, RotateCcw, Trash2, Copy, Undo, Redo,
  Square, LogOut, Play, Pause, Shield, MapPin, Clock,
  X, HelpCircle, MessageSquare, User, ChevronRight, Activity,
  Maximize2, ChevronLeft, Users, Cloud, BarChart3, Eye, Sliders, Zap
} from 'lucide-react';
import { parseWKT, parseJSONWKT, parseDXF, parseIFC } from './utils/parsers';

function App() {
  const [showModal, setShowModal] = useState(true);
  const [activeTool, setActiveTool] = useState('Select');
  const [elements, setElements] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [drawingPoints, setDrawingPoints] = useState([]);
  const [history, setHistory] = useState([]);
  const [journeySource, setJourneySource] = useState(null);

  // Simulation & Playback State
  const [agents, setAgents] = useState([]);
  const [isSimulating, setIsSimulating] = useState(false);
  const [calculationProgress, setCalculationProgress] = useState(0);
  const [playbackMode, setPlaybackMode] = useState(false);
  const [currentFrame, setCurrentFrame] = useState(0);
  const [totalFrames, setTotalFrames] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [simDuration, setSimDuration] = useState(300);
  const [showRightPanel, setShowRightPanel] = useState(true);
  const [ambientTemp, setAmbientTemp] = useState(20.0);
  const [heatmapData, setHeatmapData] = useState(null);
  const [showHeatmap, setShowHeatmap] = useState(true);
  const [showSmoke, setShowSmoke] = useState(true);
  const [showConfigPanel, setShowConfigPanel] = useState(true);
  const [oceanComposition, setOceanComposition] = useState({ openness: 20, conscientiousness: 20, extraversion: 20, agreeableness: 20, neuroticism: 20 });
  const [scenarioType, setScenarioType] = useState('Smoke');
  const [loiterMode, setLoiterMode] = useState(false);
  const [emergencyMode, setEmergencyMode] = useState(false);
  const [emergencyTriggerTime, setEmergencyTriggerTime] = useState(30);

  const [leftTab, setLeftTab] = useState('Crowd');
  const [rightTab, setRightTab] = useState('Visuals');

  // Viz Options
  const [showTrails, setShowTrails] = useState(true);
  const [colorMode, setColorMode] = useState('Uniform Color');
  const [trailColorMode, setTrailColorMode] = useState('Travel Distance');
  const [agentTrails, setAgentTrails] = useState(new Map()); // id -> points[]

  const wsRef = useRef(null);
  const canvasRef = useRef(null);
  const fileInputRef = useRef(null);

  const saveToHistory = (currentElements) => {
    setHistory(prev => [...prev.slice(-19), JSON.stringify(currentElements)]);
  };

  const undo = () => {
    if (history.length === 0) return;
    const last = history[history.length - 1];
    setHistory(prev => prev.slice(0, -1));
    setElements(JSON.parse(last));
    setSelectedId(null);
  };

  const isPointInPoly = (p, poly) => {
    let inside = false;
    for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
      const xi = poly[i].x, yi = poly[i].y;
      const xj = poly[j].x, yj = poly[j].y;
      const intersect = ((yi > p.y) !== (yj > p.y)) &&
        (p.x < (xj - xi) * (p.y - yi) / (yj - yi) + xi);
      if (intersect) inside = !inside;
    }
    return inside;
  };

  const startSimulation = () => {
    if (isSimulating) return;
    const ws = new WebSocket('ws://localhost:8000/ws/simulation');
    wsRef.current = ws;
    ws.onopen = () => {
      ws.send(JSON.stringify({
        action: 'calculate',
        config: {
          elements: elements,
          fps: 20,
          duration: simDuration,
          ambientTemperature: ambientTemp,
          crowdComposition: { male: 50, female: 50, child: 0 },
          oceanComposition: oceanComposition,
          loiterMode: loiterMode,
          emergencyMode: emergencyMode,
          emergencyTriggerTime: emergencyTriggerTime
        }
      }));
      setIsSimulating(true);
      setPlaybackMode(false);
      setCalculationProgress(0);
      setAgentTrails(new Map());
    };
    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.type === 'progress') {
        setCalculationProgress(data.percent);
        updateTrails(data.agents);
        setAgents(data.agents);
        if (data.heatmap) {
          setHeatmapData(data.heatmap);
        }
      } else if (data.type === 'finished') {
        setCalculationProgress(100);
        setPlaybackMode(true);
        ws.send(JSON.stringify({ action: 'load_recording', file: data.file }));
      } else if (data.type === 'recording_info') {
        setTotalFrames(data.num_frames);
      } else if (data.type === 'frame_data') {
        setAgents(data.agents);
        setCurrentFrame(data.frame);
        updateTrails(data.agents);
        if (data.heatmap) {
          setHeatmapData(data.heatmap);
        }
      }
    };
    ws.onclose = () => { setIsSimulating(false); setIsPlaying(false); };
  };

  const updateTrails = (newAgents) => {
    if (!showTrails) return;
    setAgentTrails(prev => {
      const next = new Map(prev);
      newAgents.forEach(a => {
        const pts = next.get(a.id) || [];
        let totalDist = pts.length > 0 ? pts[pts.length - 1].totalDist || 0 : 0;
        if (pts.length > 0) {
          const last = pts[pts.length - 1];
          totalDist += Math.hypot(a.x - last.x, a.y - last.y);
        }
        next.set(a.id, [...pts, { x: a.x, y: a.y, fatigue: a.fatigue, totalDist: totalDist, target: { x: a.target_x, y: a.target_y } }]);
      });
      return next;
    });
  };

  const stopSimulation = () => {
    if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    setIsSimulating(false);
    setPlaybackMode(false);
    setAgents([]);
    setIsPlaying(false);
    setCurrentFrame(0);
    setAgentTrails(new Map());
  };

  useEffect(() => {
    let timer;
    if (isPlaying && playbackMode && wsRef.current) {
      timer = setInterval(() => {
        const nextFrame = (currentFrame + 1) % totalFrames;
        wsRef.current.send(JSON.stringify({ action: 'get_frame', frame: nextFrame }));
        if (nextFrame === 0) { setIsPlaying(false); setAgentTrails(new Map()); }
      }, 50);
    }
    return () => clearInterval(timer);
  }, [isPlaying, playbackMode, currentFrame, totalFrames]);

  const seekFrame = (frameIdx) => {
    if (wsRef.current && playbackMode) {
      if (frameIdx < currentFrame) setAgentTrails(new Map()); // Reset trails on seek back
      wsRef.current.send(JSON.stringify({ action: 'get_frame', frame: frameIdx }));
    }
  };

  const [transform, setTransform] = useState({ x: 0, y: 0, scale: 20 });
  const [mousePos, setMousePos] = useState({ x: 0, y: 0 });
  const [isPanning, setIsPanning] = useState(false);
  const [startPan, setStartPan] = useState({ x: 0, y: 0 });
  const [dragStart, setDragStart] = useState(null);

  const drawArrow = (ctx, fromX, fromY, toX, toY, color) => {
    const headlen = 10; const angle = Math.atan2(toY - fromY, toX - fromX);
    ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.beginPath();
    ctx.moveTo(fromX, fromY); ctx.lineTo(toX, toY); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(toX, toY);
    ctx.lineTo(toX - headlen * Math.cos(angle - Math.PI / 6), toY - headlen * Math.sin(angle - Math.PI / 6));
    ctx.moveTo(toX, toY);
    ctx.lineTo(toX - headlen * Math.cos(angle + Math.PI / 6), toY - headlen * Math.sin(angle + Math.PI / 6));
    ctx.stroke();
  };

  const screenToSim = (clientX, clientY, ctrlKey = false) => {
    const rect = canvasRef.current.getBoundingClientRect();
    let x = (clientX - rect.left - (canvasRef.current.width / 2 + transform.x)) / transform.scale;
    let y = -(clientY - rect.top - (canvasRef.current.height / 2 + transform.y)) / transform.scale;
    if (ctrlKey) { const interval = transform.scale < 5 ? 10 : 1; x = Math.round(x / interval) * interval; y = Math.round(y / interval) * interval; }
    return { x, y };
  };

  useEffect(() => {
    const canvas = canvasRef.current; if (!canvas) return;
    const ctx = canvas.getContext('2d');
    let animationFrameId;
    const render = () => {
      if (canvas.width !== canvas.clientWidth || canvas.height !== canvas.clientHeight) { canvas.width = canvas.clientWidth; canvas.height = canvas.clientHeight; }
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      const centerX = canvas.width / 2 + transform.x; const centerY = canvas.height / 2 + transform.y;
      const step = transform.scale;

      // 0. Heatmap
      if (showHeatmap && heatmapData && heatmapData.grid) {
        const { width, height, resolution, grid, bounds } = heatmapData;

        // Create an offscreen canvas for the heatmap to allow smooth scaling
        if (!window.heatmapCanvas || window.heatmapCanvas.width !== width || window.heatmapCanvas.height !== height) {
          window.heatmapCanvas = document.createElement('canvas');
          window.heatmapCanvas.width = width;
          window.heatmapCanvas.height = height;
        }

        const hCtx = window.heatmapCanvas.getContext('2d');
        const imgData = hCtx.createImageData(width, height);

        for (let y = 0; y < height; y++) {
          for (let x = 0; x < width; x++) {
            const gridIdx = y * width + x;
            const imgIdx = ((height - 1 - y) * width + x) * 4;

            const temp = grid[gridIdx];
            const diff = Math.max(0, temp - ambientTemp);

            // Thermal camera color ramp (Black -> Blue -> Green -> Yellow -> Red)
            const intensity = Math.min(1.0, Math.sqrt(diff / 15.0));

            let r = 0, g = 0, b = 0;
            if (intensity < 0.2) { // Dark to Blue
              b = Math.round(intensity * 5 * 255);
            } else if (intensity < 0.5) { // Blue to Green
              b = Math.round(255 * (0.5 - intensity) * 3.33);
              g = Math.round((intensity - 0.2) * 3.33 * 255);
            } else if (intensity < 0.8) { // Green to Yellow
              g = 255;
              r = Math.round((intensity - 0.5) * 3.33 * 255);
            } else { // Yellow to Red
              r = 255;
              g = Math.round((1.0 - intensity) * 5 * 255);
            }

            imgData.data[imgIdx] = r;
            imgData.data[imgIdx + 1] = g;
            imgData.data[imgIdx + 2] = b;
            imgData.data[imgIdx + 3] = intensity > 0.05 ? 160 : 60; // Denser colors
          }
        }
        hCtx.putImageData(imgData, 0, 0);

        // Draw the heatmap canvas scaled to world coordinates
        ctx.save();
        ctx.globalAlpha = 0.6;
        const sw = width * resolution * step;
        const sh = height * resolution * step;
        const sx = centerX + bounds.xmin * step;
        const sy = (centerY - bounds.ymin * step) - sh;

        ctx.drawImage(window.heatmapCanvas, sx, sy, sw, sh);
        ctx.restore();
      }

      // 0b. Smoke Layer
      if (showSmoke && heatmapData && heatmapData.smoke_grid) {
        const { width, height, resolution, smoke_grid, bounds } = heatmapData;

        // Find the max smoke for normalization
        let maxSmoke = 0;
        for (let i = 0; i < smoke_grid.length; i++) {
          if (smoke_grid[i] > maxSmoke) maxSmoke = smoke_grid[i];
        }

        if (maxSmoke > 0.01) {
          if (!window.smokeCanvas || window.smokeCanvas.width !== width || window.smokeCanvas.height !== height) {
            window.smokeCanvas = document.createElement('canvas');
            window.smokeCanvas.width = width;
            window.smokeCanvas.height = height;
          }
          const sCtx = window.smokeCanvas.getContext('2d');
          const imgData = sCtx.createImageData(width, height);

          for (let y = 0; y < height; y++) {
            for (let x = 0; x < width; x++) {
              const gridIdx = y * width + x;
              const imgIdx = ((height - 1 - y) * width + x) * 4;
              const smokeVal = smoke_grid[gridIdx];
              // Normalize and apply a perceptual curve
              const normalized = Math.min(1.0, smokeVal / Math.max(maxSmoke, 1.0));
              const alpha = Math.round(Math.pow(normalized, 0.5) * 210); // max 210/255 opacity
              // Render as dark greyish smoke with slight brownish tinge
              imgData.data[imgIdx] = 40;   // R
              imgData.data[imgIdx + 1] = 38;   // G
              imgData.data[imgIdx + 2] = 36;   // B
              imgData.data[imgIdx + 3] = alpha;
            }
          }
          sCtx.putImageData(imgData, 0, 0);

          ctx.save();
          const sw = width * resolution * step;
          const sh = height * resolution * step;
          const sx = centerX + bounds.xmin * step;
          const sy = (centerY - bounds.ymin * step) - sh;
          ctx.drawImage(window.smokeCanvas, sx, sy, sw, sh);
          ctx.restore();
        }
      }

      // 1. Grid
      ctx.strokeStyle = 'rgba(255, 255, 255, 0.05)'; ctx.lineWidth = 1;
      const gridStep = step < 5 ? step * 10 : step; ctx.beginPath();
      for (let x = centerX % gridStep; x < canvas.width; x += gridStep) { ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); }
      for (let y = centerY % gridStep; y < canvas.height; y += gridStep) { ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); }
      ctx.stroke();

      // 2. Elements
      const allElements = [...elements];
      if (drawingPoints.length > 0) { allElements.push({ type: activeTool.toLowerCase(), points: [...drawingPoints, { x: Number(mousePos.x), y: Number(mousePos.y) }], isDrawing: true }); }
      allElements.forEach((el, index) => {
        if (!el.points || el.points.length === 0) return;
        if (el.type !== 'scenario' && el.type !== 'poi' && el.points.length < 2) return;
        ctx.beginPath();
        el.points.forEach((p, i) => { const sx = centerX + p.x * step; const sy = centerY - p.y * step; if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy); });
        if (!el.isDrawing) ctx.closePath();
        let strokeColor = index === selectedId ? '#ffffff' : '#3b82f6';
        let fillColor = index === selectedId ? 'rgba(255, 255, 255, 0.2)' : 'rgba(59, 130, 246, 0.1)';
        let lineDash = el.isDrawing ? [5, 5] : [];
        if (el.type === 'boundary') { strokeColor = index === selectedId ? '#ffffff' : '#a855f7'; fillColor = index === selectedId ? 'rgba(255, 255, 255, 0.2)' : 'rgba(168, 85, 247, 0.1)'; }
        else if (el.type === 'exit') { strokeColor = index === selectedId ? '#ffffff' : '#f97316'; fillColor = index === selectedId ? 'rgba(255, 255, 255, 0.2)' : 'rgba(249, 115, 22, 0.1)'; }
        else if (el.type === 'obstacle') { strokeColor = index === selectedId ? '#ffffff' : '#71717a'; fillColor = index === selectedId ? 'rgba(255, 255, 255, 0.2)' : 'rgba(113, 113, 122, 0.1)'; if (!el.isDrawing) lineDash = [4, 4]; }
        else if (el.type === 'journey') { strokeColor = el.color || '#fbbf24'; fillColor = 'transparent'; lineDash = [5, 5]; ctx.globalAlpha = 0.4; }
        else if (el.type === 'scenario') {
          const sx = centerX + el.points[0].x * step;
          const sy = centerY - el.points[0].y * step;
          ctx.beginPath(); ctx.arc(sx, sy, 8, 0, Math.PI * 2);
          ctx.fillStyle = el.scenarioType === 'Smoke' ? 'rgba(156, 163, 175, 0.8)' : (el.scenarioType === 'Fire' ? 'rgba(239, 68, 68, 0.8)' : 'rgba(245, 158, 11, 0.8)');
          ctx.fill(); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = index === selectedId ? 3 : 1; ctx.stroke();
          ctx.fillStyle = 'white'; ctx.font = '10px Inter'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(el.scenarioType[0], sx, sy);
          return;
        }
        else if (el.type === 'poi') {
          const sx = centerX + el.points[0].x * step;
          const sy = centerY - el.points[0].y * step;
          ctx.beginPath(); ctx.arc(sx, sy, 6, 0, Math.PI * 2);
          ctx.fillStyle = 'rgba(20, 184, 166, 0.8)'; // Teal
          ctx.fill(); ctx.strokeStyle = '#ffffff'; ctx.lineWidth = index === selectedId ? 3 : 1; ctx.stroke();
          ctx.fillStyle = 'white'; ctx.font = '8px Inter'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText('P', sx, sy);
          return;
        }
        ctx.setLineDash(lineDash); ctx.fillStyle = fillColor; ctx.fill(); ctx.strokeStyle = strokeColor; ctx.lineWidth = index === selectedId ? 3 : 2; ctx.stroke(); ctx.globalAlpha = 1.0;
        if (el.type === 'journey' && !el.isDrawing && el.points.length >= 2) {
          const p1 = el.points[el.points.length - 2]; const p2 = el.points[el.points.length - 1];
          const sx1 = centerX + p1.x * step; const sy1 = centerY - p1.y * step;
          const sx2 = centerX + p2.x * step; const sy2 = centerY - p2.y * step;
          ctx.setLineDash([]); ctx.globalAlpha = 0.4; drawArrow(ctx, sx1, sy1, sx2, sy2, strokeColor); ctx.globalAlpha = 1.0;
        }
      });

      // 2.5 Trails
      if (showTrails) {
        agentTrails.forEach((pts, id) => {
          if (pts.length < 2) return;
          ctx.beginPath(); ctx.setLineDash([]); ctx.lineWidth = 1.5; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.globalAlpha = 0.5;

          pts.forEach((p, i) => {
            const sx = centerX + p.x * step; const sy = centerY - p.y * step;

            if (i === 0) ctx.moveTo(sx, sy);
            else {
              if (trailColorMode === 'Travel Distance') {
                const hue = Math.min(240, (p.totalDist / 50) * 240);
                ctx.strokeStyle = `hsla(${240 - hue}, 80%, 50%, 0.5)`;
              } else if (trailColorMode === 'Distance Remaining') {
                const distToTarget = p.target ? Math.hypot(p.x - p.target.x, p.y - p.target.y) : 0;
                const hue = Math.min(240, (distToTarget / 30) * 240);
                ctx.strokeStyle = `hsla(${hue}, 80%, 50%, 0.5)`;
              } else if (trailColorMode === 'Subtle Gray') {
                ctx.strokeStyle = 'rgba(255, 255, 255, 0.15)';
              } else {
                // Same as Agent - we'd need the agent's current color, but for performance 
                // and clarity, we'll use a default blue if 'Same as Agent' isn't easily accessible
                ctx.strokeStyle = 'rgba(59, 130, 246, 0.4)';
              }
              ctx.lineTo(sx, sy);
            }
          });
          ctx.stroke(); ctx.globalAlpha = 1.0;
        });
      }

      // 3. Agents
      agents.forEach(agent => {
        const sx = centerX + agent.x * step; const sy = centerY - agent.y * step;
        const radius = (agent.type === 'child' ? 0.15 : 0.2) * step;
        const fatigue = agent.fatigue || 0;

        let color = '#3b82f6'; // Male
        if (agent.type === 'female') color = '#ec4899';
        else if (agent.type === 'child') color = '#eab308';

        if (colorMode === 'Color by Start') {
          const hue = (agent.start_id * 137.5) % 360;
          color = `hsl(${hue}, 70%, 60%)`;
        } else if (colorMode === 'Color by Exit') {
          const hue = (agent.stage_id * 137.5) % 360;
          color = `hsl(${hue}, 70%, 60%)`;
        } else if (colorMode === 'Dominant OCEAN Trait') {
          const traitColors = {
            'openness': '#3b82f6',
            'conscientiousness': '#10b981',
            'extraversion': '#f59e0b',
            'agreeableness': '#06b6d4',
            'neuroticism': '#ef4444'
          };
          color = traitColors[agent.dominant_trait] || color;
        } else if (colorMode === 'Stress Level') {
          const stress = agent.stress || 0;
          const r = Math.min(255, Math.floor(stress * 255 * 2));
          const g = Math.min(255, Math.floor((1 - stress) * 255));
          color = `rgb(${r}, ${g}, 50)`;
        } else if (colorMode === 'Panic Level') {
          console.log('Agents panic levels:', agents.map(a => ({ id: a.id, panic: a.panic })));
          const panic = Math.min(1.0, (agent.panic || 0) * 1.5);
          if (panic > 0.8) {
            color = '#ff0000'; // Bright red for high panic
          } else if (panic > 0.5) {
            color = '#ff6600'; // Orange for medium panic
          } else if (panic > 0.2) {
            color = '#ffcc00'; // Yellow for low panic
          } else {
            color = '#00ff00'; // Green for calm
          }
        } else if (colorMode === 'Heartbeat') {
          const hb = agent.heartbeat || 70;
          const normalized = Math.min(1, Math.max(0, (hb - 60) / 40)); // 60-100 range
          const r = Math.min(255, Math.floor(normalized * 255));
          const b = Math.min(255, Math.floor((1 - normalized) * 255));
          color = `rgb(${r}, 50, ${b})`;
        } else if (fatigue > 0.1) {
          const r = Math.min(255, Math.floor(fatigue * 255));
          const g = Math.min(255, Math.floor((1 - fatigue) * 255));
          color = `rgb(${r}, ${g}, 50)`;
        }

        ctx.beginPath();
        if (agent.type === 'female') {
          // Triangle
          ctx.moveTo(sx, sy - radius);
          ctx.lineTo(sx - radius * 0.9, sy + radius * 0.7);
          ctx.lineTo(sx + radius * 0.9, sy + radius * 0.7);
          ctx.closePath();
        } else if (agent.type === 'child') {
          // Square
          ctx.rect(sx - radius, sy - radius, radius * 2, radius * 2);
        } else {
          // Circle (Male)
          ctx.arc(sx, sy, radius, 0, Math.PI * 2);
        }

        ctx.fillStyle = color; ctx.fill();
        ctx.strokeStyle = 'white'; ctx.lineWidth = 1; ctx.stroke();

        // Label for clarity at low fatigue
        if (fatigue < 0.3) {
          ctx.fillStyle = 'white';
          ctx.font = `${radius * 0.7}px Inter`;
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          const char = agent.type ? agent.type[0].toUpperCase() : 'M';
          // Adjust label Y for triangle
          const labelY = agent.type === 'female' ? sy + radius * 0.2 : sy;
          ctx.fillText(char, sx, labelY);
        }
      });

      // 4. Rulers
      const rulerColor = 'rgba(255, 255, 255, 0.5)'; ctx.setLineDash([]); ctx.font = '9px Inter'; ctx.fillStyle = rulerColor; ctx.strokeStyle = 'rgba(255, 255, 255, 0.1)';
      const rulerHeight = canvas.height - 20; ctx.beginPath(); ctx.moveTo(0, rulerHeight); ctx.lineTo(canvas.width, rulerHeight);
      for (let i = Math.floor((-centerX) / step); i < Math.ceil((canvas.width - centerX) / step); i++) {
        const x = centerX + i * step; const tickHeight = i % 5 === 0 ? 10 : 5; ctx.moveTo(x, rulerHeight); ctx.lineTo(x, rulerHeight + tickHeight);
        if (i % 5 === 0) ctx.fillText(i + 'm', x + 2, rulerHeight + 18);
      }
      const rulerLeft = 20; ctx.moveTo(rulerLeft, 0); ctx.lineTo(rulerLeft, canvas.height);
      for (let i = Math.floor((centerY - canvas.height) / step); i < Math.ceil(centerY / step); i++) {
        const y = centerY - i * step; const tickWidth = i % 5 === 0 ? 10 : 5; ctx.moveTo(rulerLeft, y); ctx.lineTo(rulerLeft - tickWidth, y);
        if (i % 5 === 0) ctx.fillText(i + 'm', 2, y - 2);
      }
      ctx.stroke(); animationFrameId = requestAnimationFrame(render);
    };
    render();
    return () => cancelAnimationFrame(animationFrameId);
  }, [transform, elements, drawingPoints, mousePos, activeTool, selectedId, agents, showTrails, colorMode, agentTrails, showHeatmap, showSmoke, heatmapData, ambientTemp]);

  const deleteSelected = () => { if (selectedId === null) return; saveToHistory(elements); setElements(prev => prev.filter((_, i) => i !== selectedId)); setSelectedId(null); };
  const finalizeDrawing = () => {
    if (drawingPoints.length > 2) {
      saveToHistory(elements);
      const type = activeTool.toLowerCase();
      const newEl = {
        id: elements.length,
        type,
        points: drawingPoints,
        agentCount: type === 'start' ? 10 : 0
      };
      if (type === 'start') {
        newEl.crowdComposition = { male: 50, female: 50, child: 0 };
      }
      setElements(prev => [...prev, newEl]);
    }
    setDrawingPoints([]);
  };

  const handleWheel = (e) => { const zoomIntensity = 0.1; const delta = e.deltaY > 0 ? 1 - zoomIntensity : 1 + zoomIntensity; setTransform(prev => ({ ...prev, scale: Math.min(Math.max(prev.scale * delta, 2), 200) })); };
  const handleMouseDown = (e) => {
    const pos = screenToSim(e.clientX, e.clientY, e.ctrlKey);
    if (e.button === 1) { setIsPanning(true); setStartPan({ x: e.clientX - transform.x, y: e.clientY - transform.y }); return; }
    if (e.button === 0) {
      if (activeTool === 'Select') {
        let foundIndex = null;
        for (let i = elements.length - 1; i >= 0; i--) { if (isPointInPoly(pos, elements[i].points)) { foundIndex = i; break; } }
        setSelectedId(foundIndex);
        if (foundIndex !== null) setLeftTab('Selection');
      }
      else if (activeTool === 'Move') {
        if (selectedId !== null && isPointInPoly(pos, elements[selectedId].points)) setDragStart({ x: pos.x, y: pos.y });
        else {
          setIsPanning(true);
          setStartPan({ x: e.clientX - transform.x, y: e.clientY - transform.y });
        }
      }
      else if (activeTool === 'Journey') {
        let foundIndex = null; for (let i = elements.length - 1; i >= 0; i--) { if (isPointInPoly(pos, elements[i].points)) { foundIndex = i; break; } }
        if (foundIndex !== null) {
          if (journeySource === null) setJourneySource(foundIndex);
          else if (journeySource !== foundIndex) {
            const sEl = elements[journeySource]; const dEl = elements[foundIndex];
            const sMid = { x: sEl.points.reduce((acc, p) => acc + p.x, 0) / sEl.points.length, y: sEl.points.reduce((acc, p) => acc + p.y, 0) / sEl.points.length };
            const dMid = { x: dEl.points.reduce((acc, p) => acc + p.x, 0) / dEl.points.length, y: dEl.points.reduce((acc, p) => acc + p.y, 0) / dEl.points.length };
            saveToHistory(elements); setElements(prev => [...prev, { type: 'journey', points: [sMid, dMid], color: `hsl(${Math.random() * 360}, 70%, 60%)` }]); setJourneySource(null);
          }
        }
      } else if (activeTool === 'ScenarioPoint') {
        saveToHistory(elements);
        setElements(prev => [...prev, { id: elements.length, type: 'scenario', scenarioType: scenarioType, points: [{ x: Number(pos.x), y: Number(pos.y) }] }]);
        setActiveTool('Select');
      } else if (activeTool === 'POI') {
        saveToHistory(elements);
        setElements(prev => [...prev, { id: elements.length, type: 'poi', points: [{ x: Number(pos.x), y: Number(pos.y) }] }]);
        setActiveTool('Select');
      } else if (['Boundary', 'Exit', 'Obstacle', 'Start'].includes(activeTool)) setDrawingPoints(prev => [...prev, pos]);
    }
  };

  const handleMouseMove = (e) => {
    const pos = screenToSim(e.clientX, e.clientY, e.ctrlKey); setMousePos({ x: pos.x.toFixed(2), y: pos.y.toFixed(2) });
    if (isPanning) setTransform(prev => ({ ...prev, x: e.clientX - startPan.x, y: e.clientY - startPan.y }));
    else if (dragStart) {
      const dx = pos.x - dragStart.x; const dy = pos.y - dragStart.y;
      setElements(prev => prev.map((el, i) => i === selectedId ? { ...el, points: el.points.map(p => ({ x: p.x + dx, y: p.y + dy })) } : el));
      setDragStart({ x: pos.x, y: pos.y });
    }
  };

  const handleMouseUp = () => { if (dragStart) saveToHistory(elements); setIsPanning(false); setDragStart(null); };
  const handleContextMenu = (e) => { e.preventDefault(); finalizeDrawing(); };

  const onFileImport = (e) => {
    const file = e.target.files[0]; if (!file) return;
    const reader = new FileReader(); reader.onload = (event) => {
      const content = event.target.result; let imported = []; const extension = file.name.split('.').pop().toLowerCase();
      if (extension === 'json') imported = parseJSONWKT(content) || [];
      else if (extension === 'wkt') { const shape = parseWKT(content); if (shape) imported = [shape]; }
      else if (extension === 'dxf') imported = parseDXF(content) || [];
      else if (extension === 'ifc') imported = parseIFC(content) || [];
      if (imported.length > 0) {
        const validImported = imported.filter(Boolean); setElements(prev => [...prev, ...validImported]); setShowModal(false);
        if (validImported.length > 0) {
          const allPoints = validImported.flatMap(el => el.points);
          const minX = Math.min(...allPoints.map(p => p.x)); const maxX = Math.max(...allPoints.map(p => p.x));
          const minY = Math.min(...allPoints.map(p => p.y)); const maxY = Math.max(...allPoints.map(p => p.y));
          setTransform(prev => ({ ...prev, x: -((minX + maxX) / 2) * prev.scale, y: ((minY + maxY) / 2) * prev.scale }));
        }
      }
    };
    reader.readAsText(file);
  };

  return (
    <div className="app-wrapper">
      <input type="file" ref={fileInputRef} style={{ display: 'none' }} onChange={onFileImport} accept=".wkt,.json,.dxf,.ifc" />
      <header className="top-toolbar">
        <div className="toolbar-group"> <button className="toolbar-btn">Settings</button> <button className="toolbar-btn" onClick={() => fileInputRef.current.click()}>Import</button> </div>
        <div className="toolbar-group">
          <button className={`toolbar-btn ${activeTool === 'Select' ? 'active' : ''}`} onClick={() => setActiveTool('Select')}><MousePointer2 size={14} /> Select</button>
          <button className={`toolbar-btn ${activeTool === 'Move' ? 'active' : ''}`} onClick={() => setActiveTool('Move')}><Move size={14} /> Move</button>
          <button className="toolbar-btn" onClick={deleteSelected}><Trash2 size={14} /> Delete</button>
          <button className="toolbar-btn clear-all-btn" onClick={() => { saveToHistory(elements); setElements([]); }}><RotateCcw size={14} /> Clear All</button>
          <button className="toolbar-btn" onClick={undo}><Undo size={14} /> Undo</button>
        </div>
        <div className="spacer" />
        <div className="toolbar-group">
          <button className={`toolbar-btn ${activeTool === 'Boundary' ? 'active' : ''}`} onClick={() => { setActiveTool('Boundary'); setJourneySource(null); }}>Boundary</button>
          <button className={`toolbar-btn ${activeTool === 'Exit' ? 'active' : ''}`} onClick={() => { setActiveTool('Exit'); setJourneySource(null); }}>Exit</button>
          <button className={`toolbar-btn ${activeTool === 'Obstacle' ? 'active' : ''}`} onClick={() => { setActiveTool('Obstacle'); setJourneySource(null); }}>Obstacle</button>
          <button className={`toolbar-btn ${activeTool === 'Start' ? 'active' : ''}`} onClick={() => { setActiveTool('Start'); setJourneySource(null); }}>Start</button>
          <button className={`toolbar-btn ${activeTool === 'Journey' ? 'active' : ''}`} onClick={() => { setActiveTool('Journey'); setJourneySource(null); }}>Journey</button>
          <button className={`toolbar-btn ${activeTool === 'POI' ? 'active' : ''}`} onClick={() => { setActiveTool('POI'); setJourneySource(null); }}>POI</button>
        </div>
        <div className="toolbar-group" style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <div className="duration-input" style={{ display: 'flex', alignItems: 'center', background: 'var(--glass)', border: '1px solid var(--border-light)', borderRadius: '4px', padding: '0 8px' }}>
            <Clock size={12} color="#aaa" />
            <input type="number" value={simDuration} onChange={(e) => setSimDuration(parseInt(e.target.value))} style={{ width: '50px', background: 'none', border: 'none', color: 'white', fontSize: '11px', padding: '4px' }} />
            <span style={{ fontSize: '10px', color: '#888' }}>sec</span>
          </div>
          <button className={`toolbar-btn ${isSimulating && !playbackMode ? 'active' : ''}`} style={{ backgroundColor: (isSimulating && !playbackMode) ? '#ef4444' : '#3b82f6' }} onClick={isSimulating ? stopSimulation : startSimulation}>{(isSimulating && !playbackMode) ? 'Stop' : 'Calculate'}</button>
        </div>
        <div className="user-profile"><span style={{ fontSize: '11px' }}>LU</span><div className="avatar"><User size={12} color="white" /></div></div>
      </header>
      <main className="viewport-container">
        <div className={`config-sidebar ${showConfigPanel ? 'open' : 'closed'}`}>
          <button className="panel-toggle" onClick={() => setShowConfigPanel(!showConfigPanel)}> {showConfigPanel ? <ChevronLeft size={16} /> : <ChevronRight size={16} />} </button>

          <div className="sidebar-tabs">
            <button className={`tab-btn ${leftTab === 'Crowd' ? 'active' : ''}`} onClick={() => setLeftTab('Crowd')}>
              <Users size={14} /> Crowd
            </button>
            <button className={`tab-btn ${leftTab === 'Selection' ? 'active' : ''}`} onClick={() => setLeftTab('Selection')}>
              <Sliders size={14} /> Selection
            </button>
            <button className={`tab-btn ${leftTab === 'Scenario' ? 'active' : ''}`} onClick={() => setLeftTab('Scenario')}>
              <Zap size={14} /> Scenario
            </button>
          </div>

          <div className="sidebar-content">
            {leftTab === 'Crowd' && (
              <>
                <div className="panel-section">
                  <h3><MapPin size={16} color="var(--accent)" /> Start Areas</h3>
                  <div className="start-areas-list" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {elements.filter(el => el.type === 'start').map((el, i) => (
                      <button key={el.id} className={`toolbar-btn ${selectedId === el.id ? 'active' : ''}`}
                        style={{ justifyContent: 'space-between', width: '100%', padding: '10px' }}
                        onClick={() => { setSelectedId(el.id); setLeftTab('Selection'); }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <Shield size={12} />
                          <span>Start Area {el.id}</span>
                        </div>
                        <div style={{ fontSize: '10px', color: 'var(--text-muted)' }}>
                          {el.agentCount || 10} agents
                        </div>
                      </button>
                    ))}
                    {elements.filter(el => el.type === 'start').length === 0 && (
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)', textAlign: 'center', padding: '10px', background: 'rgba(255,255,255,0.02)', borderRadius: '6px' }}>
                        No start areas defined
                      </div>
                    )}
                  </div>
                </div>

                <div className="panel-section">
                  <h3><Activity size={16} color="var(--accent)" /> OCEAN Traits</h3>
                  {Object.keys(oceanComposition).map(trait => (
                    <div className="panel-field" key={trait}>
                      <label style={{ textTransform: 'capitalize' }}>{trait} (%)</label>
                      <input type="range" min="0" max="100" value={oceanComposition[trait]} onChange={(e) => {
                        setOceanComposition(prev => ({ ...prev, [trait]: parseInt(e.target.value) }));
                      }} />
                      <div style={{ fontSize: '10px', textAlign: 'right' }}>{oceanComposition[trait]}%</div>
                    </div>
                  ))}
                </div>
              </>
            )}

            {leftTab === 'Selection' && (
              <div className="panel-section">
                {selectedId !== null ? (
                  <>
                    {elements.find(el => el.id === selectedId)?.type === 'start' ? (
                      <>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '15px', color: '#3b82f6' }}>
                          <Shield size={16} />
                          <h3 style={{ margin: 0 }}>Start Area {selectedId}</h3>
                        </div>
                        <div className="panel-field">
                          <label>Agent Count</label>
                          <input type="number" value={elements.find(el => el.id === selectedId).agentCount || 10} onChange={(e) => {
                            const val = parseInt(e.target.value) || 0;
                            setElements(prev => prev.map(el => el.id === selectedId ? { ...el, agentCount: val } : el));
                          }} />
                        </div>

                        <h4 style={{ fontSize: '12px', color: 'var(--text-main)', marginTop: '20px', marginBottom: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <Users size={14} color="var(--accent)" /> Crowd Composition
                        </h4>
                        {['male', 'female', 'child'].map(type => (
                          <div className="panel-field" key={type}>
                            <label style={{ textTransform: 'capitalize' }}>{type} (%)</label>
                            <input type="range" min="0" max="100"
                              value={elements.find(el => el.id === selectedId).crowdComposition?.[type] || 0}
                              onChange={(e) => {
                                const val = parseInt(e.target.value);
                                const el = elements.find(el => el.id === selectedId);
                                const currentComp = el.crowdComposition || { male: 50, female: 50, child: 0 };
                                const newComp = { ...currentComp, [type]: val };
                                setElements(prev => prev.map(e => e.id === selectedId ? { ...e, crowdComposition: newComp } : e));
                              }}
                            />
                            <div style={{ fontSize: '10px', textAlign: 'right' }}>
                              {elements.find(el => el.id === selectedId).crowdComposition?.[type] || 0}%
                            </div>
                          </div>
                        ))}
                        <div style={{ fontSize: '10px', color: '#888', marginBottom: '10px' }}>
                          Configure demographics for this specific entry point.
                        </div>
                      </>
                    ) : (
                      <div style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--text-muted)' }}>
                        <Box size={32} style={{ marginBottom: '16px', opacity: 0.5 }} />
                        <p>Element {selectedId} selected. No specific properties available.</p>
                      </div>
                    )}
                  </>
                ) : (
                  <div style={{ textAlign: 'center', padding: '40px 20px', color: 'var(--text-muted)' }}>
                    <MousePointer2 size={32} style={{ marginBottom: '16px', opacity: 0.5 }} />
                    <p>Select an element on the canvas to view its properties.</p>
                  </div>
                )}
              </div>
            )}

            {leftTab === 'Scenario' && (
              <div className="panel-section">
                <h3><Zap size={16} color="var(--accent)" /> Scenarios</h3>
                <div className="panel-field">
                  <label>Type</label>
                  <select value={scenarioType} onChange={(e) => setScenarioType(e.target.value)}>
                    <option>Smoke</option>
                    <option>Fire</option>
                    <option>Burst</option>
                  </select>
                </div>
                <button className={`toolbar-btn ${activeTool === 'ScenarioPoint' ? 'active' : ''}`} onClick={() => setActiveTool('ScenarioPoint')} style={{ width: '100%', marginBottom: '15px', justifyContent: 'center' }}>
                  <MapPin size={14} style={{ marginRight: '8px' }} /> Place Source
                </button>

                <h4 style={{ fontSize: '12px', color: 'var(--text-main)', marginTop: '20px', marginBottom: '8px' }}>Active Sources</h4>
                <div className="start-areas-list" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {elements.filter(el => el.type === 'scenario').map((el, i) => (
                    <button key={el.id} className="toolbar-btn" style={{ justifyContent: 'space-between', width: '100%', padding: '10px' }} onClick={() => { setSelectedId(el.id); setLeftTab('Selection'); }}>
                      <span>{el.scenarioType} Source</span>
                      <Trash2 size={12} onClick={(e) => { e.stopPropagation(); setElements(prev => prev.filter(e => e.id !== el.id)); }} />
                    </button>
                  ))}
                  {elements.filter(el => el.type === 'scenario').length === 0 && (
                    <div style={{ fontSize: '11px', color: 'var(--text-muted)', textAlign: 'center', padding: '10px', background: 'rgba(255,255,255,0.02)', borderRadius: '6px' }}>
                      No active sources
                    </div>
                  )}
                </div>

                <div className="panel-section" style={{ marginTop: '20px', padding: 0, border: 'none' }}>
                  <h4 style={{ fontSize: '12px', color: 'var(--text-main)', marginBottom: '8px' }}>Simulation Modes</h4>
                  <div className="panel-field">
                    <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                      <input type="checkbox" checked={loiterMode} onChange={(e) => setLoiterMode(e.target.checked)} />
                      <span>Loiter Mode (Wander POIs)</span>
                    </label>
                  </div>
                  <div className="panel-field" style={{ marginTop: '10px' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer' }}>
                      <input type="checkbox" checked={emergencyMode} onChange={(e) => setEmergencyMode(e.target.checked)} />
                      <span>Emergency Mode (Fire Alarm)</span>
                    </label>
                  </div>
                  {emergencyMode && (
                    <div className="panel-field" style={{ marginTop: '10px', marginLeft: '24px' }}>
                      <label>Trigger Time (s)</label>
                      <input type="range" min="0" max={simDuration} value={emergencyTriggerTime} onChange={(e) => setEmergencyTriggerTime(parseInt(e.target.value))} />
                      <div style={{ fontSize: '10px', textAlign: 'right' }}>{emergencyTriggerTime}s</div>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
        <canvas ref={canvasRef} onWheel={handleWheel} onMouseDown={handleMouseDown} onMouseMove={handleMouseMove} onMouseUp={handleMouseUp} onMouseLeave={handleMouseUp} onContextMenu={handleContextMenu} style={{ cursor: activeTool === 'Move' ? 'grab' : 'crosshair' }} />
        <div className={`viz-sidebar ${showRightPanel ? 'open' : 'closed'}`}>
          <button className="panel-toggle" onClick={() => setShowRightPanel(!showRightPanel)}> {showRightPanel ? <ChevronRight size={16} /> : <ChevronLeft size={16} />} </button>

          <div className="sidebar-tabs">
            <button className={`tab-btn ${rightTab === 'Visuals' ? 'active' : ''}`} onClick={() => setRightTab('Visuals')}>
              <Eye size={14} /> Visuals
            </button>
            <button className={`tab-btn ${rightTab === 'Environment' ? 'active' : ''}`} onClick={() => setRightTab('Environment')}>
              <Cloud size={14} /> Env
            </button>
            <button className={`tab-btn ${rightTab === 'Stats' ? 'active' : ''}`} onClick={() => setRightTab('Stats')}>
              <BarChart3 size={14} /> Stats
            </button>
          </div>

          <div className="sidebar-content">
            {rightTab === 'Visuals' && (
              <div className="panel-section">
                <h3><Zap size={16} color="var(--accent)" /> Agent Coloring</h3>
                <div className="panel-field">
                  <label>Color Mode</label>
                  <select value={colorMode} onChange={(e) => setColorMode(e.target.value)}>
                    <option>Uniform Color</option>
                    <option>Color by Start</option>
                    <option>Color by Exit</option>
                    <option>Dominant OCEAN Trait</option>
                    <option>Stress Level</option>
                    <option>Panic Level</option>
                    <option>Heartbeat</option>
                    <option>Travel Distance (Total)</option>
                    <option>Distance Remaining</option>
                  </select>
                </div>
                <div className="panel-toggle-group">
                  <label className="toggle-item"> <input type="checkbox" checked={showTrails} onChange={(e) => setShowTrails(e.target.checked)} /> <span>Agent Trails</span> </label>
                  {showTrails && (
                    <div className="panel-field" style={{ marginLeft: '24px', marginTop: '8px' }}>
                      <label>Trail Color Mode</label>
                      <select value={trailColorMode} onChange={(e) => setTrailColorMode(e.target.value)} style={{ fontSize: '11px', padding: '4px' }}>
                        <option>Travel Distance</option>
                        <option>Distance Remaining</option>
                        <option>Subtle Gray</option>
                        <option>Standard Blue</option>
                      </select>
                    </div>
                  )}
                  <label className="toggle-item"> <input type="checkbox" /> <span>Contour Overlay</span> </label>
                  <label className="toggle-item"> <input type="checkbox" /> <span>Stage Labels</span> </label>
                </div>
                {colorMode === 'Travel Distance (Total)' && (<div className="legend-container"> <div className="legend-bar" /> <div className="legend-labels"> <span>0.0</span> <span>23.6 m</span> </div> </div>)}
                {trailColorMode === 'Travel Distance' && showTrails && (<div className="legend-container" style={{ marginTop: '10px' }}> <div className="legend-bar" /> <div className="legend-labels"> <span>Short Trail</span> <span>Long Trail</span> </div> </div>)}
              </div>
            )}

            {rightTab === 'Environment' && (
              <div className="panel-section">
                <h3><Cloud size={16} color="var(--accent)" /> Weather Panel</h3>
                <div className="panel-field">
                  <label>Ambient Temp (°C)</label>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <input
                      type="range"
                      min="-10"
                      max="50"
                      value={ambientTemp}
                      onChange={(e) => setAmbientTemp(parseFloat(e.target.value))}
                      style={{ flex: 1 }}
                    />
                    <span style={{ minWidth: '40px', textAlign: 'right' }}>{ambientTemp}°</span>
                  </div>
                </div>
                <div className="panel-toggle-group">
                  <label className="toggle-item">
                    <input type="checkbox" checked={showHeatmap} onChange={(e) => setShowHeatmap(e.target.checked)} />
                    <span>Show Heat Map</span>
                  </label>
                  <label className="toggle-item">
                    <input type="checkbox" checked={showSmoke} onChange={(e) => setShowSmoke(e.target.checked)} />
                    <span>Show Smoke</span>
                  </label>
                </div>
                {showHeatmap && (
                  <div className="legend-container">
                    <div className="legend-bar" style={{ background: 'linear-gradient(to right, blue, green, yellow, red)' }} />
                    <div className="legend-labels">
                      <span>{ambientTemp}°C</span>
                      <span>{ambientTemp + 20}°C</span>
                    </div>
                  </div>
                )}
                {showSmoke && (
                  <div className="legend-container" style={{ marginTop: '8px' }}>
                    <div className="legend-bar" style={{ background: 'linear-gradient(to right, rgba(40,38,36,0), rgba(40,38,36,0.5), rgba(40,38,36,1))' }} />
                    <div className="legend-labels">
                      <span>No Smoke</span>
                      <span>Dense Smoke</span>
                    </div>
                  </div>
                )}
              </div>
            )}

            {rightTab === 'Stats' && (
              <div className="panel-section">
                <h3><BarChart3 size={16} color="var(--accent)" /> Exit Stats</h3>
                <table className="stats-table">
                  <thead>
                    <tr>
                      <th>EXIT ▲</th>
                      <th>COUNT</th>
                      <th>FLOW (S⁻¹)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {elements.filter(el => el.type === 'exit').map((el, i) => (
                      <tr key={i}>
                        <td>Exit {i}</td>
                        <td>0/{agents.length || 10}</td>
                        <td>0.00</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
        {showModal && (<div className="modal-overlay"><div className="modal-content"><button className="modal-close" onClick={() => setShowModal(false)}><X size={20} /></button><h2 className="modal-title">JuPedSim Web</h2><p className="modal-description">Import a scenario or draw from scratch.</p><div className="modal-options"><div className="option-card" onClick={() => fileInputRef.current.click()}>Upload File</div><div className="option-card" onClick={() => setShowModal(false)}>Start Fresh</div></div></div></div>)}
        {isSimulating && calculationProgress < 100 && (
          <div className="calculation-overlay">
            <div className="calculation-card">
              <div className="spinner" />
              <div style={{ color: 'white', marginBottom: '10px', fontSize: '18px', fontWeight: '600' }}>Calculating Scenario...</div>
              <div className="progress-bar-container"> <div className="progress-bar-fill" style={{ width: `${calculationProgress}%` }} /> </div>
              <div style={{ color: 'var(--text-muted)', fontSize: '14px' }}>{calculationProgress}% Complete</div>
            </div>
          </div>
        )}
        {playbackMode && (
          <div className="playback-controls">
            <button onClick={() => setIsPlaying(!isPlaying)} className="play-btn">{isPlaying ? <Pause size={24} /> : <Play size={24} />}</button>
            <div className="scrubber-container">
              <input type="range" min="0" max={totalFrames - 1} value={currentFrame} onChange={(e) => seekFrame(parseInt(e.target.value))} />
              <div className="time-labels"> <span>{Math.floor(currentFrame / 20)}s</span> <span>Frame {currentFrame} / {totalFrames}</span> <span>{Math.floor(totalFrames / 20)}s</span> </div>
            </div>
            <button onClick={stopSimulation} className="exit-btn">Exit</button>
          </div>
        )}
        <div className="floating-actions"><div className="fab secondary"><HelpCircle size={20} /></div><div className="fab"><MessageSquare size={20} /></div></div>
      </main>
      <footer className="status-bar">
        <div className="cursor-coords"><Activity size={12} /><span>X: {mousePos.x} m</span><span>Y: {mousePos.y} m</span></div>
        <div className="zoom-indicator"><Maximize2 size={12} /><span>Zoom: {Math.round((transform.scale / 20) * 100)}%</span></div>
        <div className="spacer" />
        {isSimulating && calculationProgress === 100 && <div className="status-item" style={{ color: '#10b981', fontWeight: 'bold', marginRight: '20px' }}>Recording Ready ({Math.floor(totalFrames / 20)}s)</div>}
        <div className="status-item">Ready</div>
      </footer>
    </div>
  );
}

export default App;
