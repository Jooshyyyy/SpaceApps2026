import React from 'react';
import {AbsoluteFill, Easing, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import './style.css';

const enter = (frame: number, start: number, duration = 22) =>
  interpolate(frame, [start, start + duration], [0, 1], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp', easing: Easing.out(Easing.cubic)});

const Label: React.FC<{children: React.ReactNode; x: number; y: number; delay?: number}> = ({children, x, y, delay = 0}) => {
  const f = useCurrentFrame();
  const a = enter(f, delay);
  return <div className="label" style={{left: x, top: y, opacity: a, transform: `translateY(${(1 - a) * 12}px)`}}>{children}</div>;
};

const Metric: React.FC<{value: string; label: string; delay: number; accent?: string}> = ({value, label, delay, accent = '#78e3ff'}) => {
  const f = useCurrentFrame();
  const a = enter(f, delay);
  return <div className="metric" style={{opacity: a, transform: `scale(${0.92 + 0.08 * a})`}}>
    <b style={{color: accent}}>{value}</b><span>{label}</span>
  </div>;
};

export const MarsEvaDemo: React.FC = () => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const phase = frame < 150 ? 0 : frame < 360 ? 1 : frame < 600 ? 2 : 3;
  const roverX = interpolate(frame, [390, 570], [180, 1000], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'});
  const scan = (frame * 2.2) % 1280;
  const pulse = 0.6 + 0.4 * Math.sin(frame / 10);
  const titleScale = spring({frame, fps, config: {damping: 180}});

  return <AbsoluteFill className="scene">
    <div className="stars" />
    <div className="planet" />
    <div className="topbar"><span>ASTRAEVA // MARS EVA SAFETY</span><span className="live">● LIVE MISSION MODEL</span></div>

    {phase === 0 && <>
      <div className="hero" style={{opacity: enter(frame, 8), transform: `scale(${0.92 + titleScale * 0.08})`}}>
        <div className="eyebrow">NASA SPACE APPS · INTERPLANETARY SURVIVAL GUIDE</div>
        <h1>WALK MARS.<br/><em>KNOW THE RISK.</em></h1>
        <p>Route intelligence for a safe astronaut EVA near Curiosity's real traverse.</p>
      </div>
      <div className="count">30 SECOND MISSION BRIEF</div>
    </>}

    {phase === 1 && <>
      <div className="sectionTitle" style={{opacity: enter(frame, 160)}}><span>01</span> THE EVA ZONE</div>
      <div className="map">
        <div className="contour c1"/><div className="contour c2"/><div className="contour c3"/>
        <div className="route-line"/>
        <div className="landing">●</div><div className="current">●</div>
        <Label x={105} y={385} delay={190}>CURIOUSITY 2025<br/><small>83 documented positions</small></Label>
        <Label x={705} y={180} delay={210}>1 km² EVA AOI<br/><small>Gale Crater, Mars</small></Label>
      </div>
      <div className="metricsRow"><Metric value="1,000 × 1,000" label="terrain cells" delay={220}/><Metric value="785,559" label="walkable cells" delay={240}/><Metric value="21.4%" label="steep / blocked" delay={260} accent="#ffb56b"/></div>
    </>}

    {phase === 2 && <>
      <div className="sectionTitle" style={{opacity: enter(frame, 370)}}><span>02</span> MULTI-LAYER PATHFINDING</div>
      <div className="terrainPanel">
        <div className="scan" style={{left: scan}}/>
        <div className="hazard h1"/><div className="hazard h2"/><div className="hazard h3"/>
        <svg viewBox="0 0 1100 300" className="pathSvg"><path d="M70 235 C180 205 218 96 334 120 S510 255 620 190 S785 74 900 132 S1000 194 1050 70"/></svg>
        <div className="rover" style={{left: roverX, top: 355, filter: `drop-shadow(0 0 ${10 + 10 * pulse}px #78e3ff)`}}>◆</div>
        <Label x={780} y={270} delay={420}>RADIATION AVOIDANCE</Label>
        <Label x={290} y={460} delay={450}>LOOSE REGOLITH PENALTY</Label>
      </div>
      <div className="metricsRow"><Metric value="0.0328" label="mSv/h RAD baseline" delay={470}/><Metric value="139.5" label="thermal inertia SI" delay={490} accent="#ffce78"/><Metric value="LOW / LOOSE" label="terrain traction" delay={510} accent="#ff8a78"/></div>
    </>}

    {phase === 3 && <>
      <div className="sectionTitle" style={{opacity: enter(frame, 610)}}><span>03</span> TELEMETRY THAT PROTECTS</div>
      <div className="hud">
        <div className="hudHead">EVA-01 · CURIOUSITY EVA SITE <i>LINK STABLE</i></div>
        <div className="gauge"><span>O₂</span><div><i style={{width: `${interpolate(frame, [620, 850], [98, 82], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})}%`}}/></div><b>82%</b></div>
        <div className="gauge"><span>POWER</span><div><i style={{width: `${interpolate(frame, [620, 850], [99, 86], {extrapolateLeft: 'clamp', extrapolateRight: 'clamp'})}%`, background: '#ffce78'}}/></div><b>86%</b></div>
        <div className="telemetry"><div><small>RETURN TIME</small><strong>01:27</strong></div><div><small>DOSE</small><strong>0.003 mSv</strong></div><div><small>ROUTE</small><strong className="safe">SAFE</strong></div></div>
      </div>
      <div className="closing" style={{opacity: enter(frame, 740)}}><b>LOCAL DATA.</b> <span>REAL TERRAIN.</span> SAFE EVA DECISIONS.</div>
      <div className="cta" style={{opacity: enter(frame, 790)}}>ASTRAEVA · INTERPLANETARY SURVIVAL GUIDE</div>
    </>}
  </AbsoluteFill>;
};
