import { useEffect, useState, useRef } from 'react';
import './App.css';

const WS_URL = 'ws://localhost:8000/ws';

function formatGap(gap) {
  if (gap === null || gap === undefined) return '--';
  if (gap === 999.0) return 'LAPPED';
  return gap > 0 ? `+${gap.toFixed(3)}s` : 'LEADER';
}

function formatLapTime(timeS) {
  if (!timeS) return '--:--.---';
  const m = Math.floor(timeS / 60);
  const s = (timeS % 60).toFixed(3).padStart(6, '0');
  return `${m}:${s}`;
}

export default function App() {
  const [connected, setConnected] = useState(false);
  const [session, setSession] = useState({ state: 'IDLE', track: 'Unknown' });
  const [grid, setGrid] = useState({});
  const [meta, setMeta] = useState({});
  const [triggers, setTriggers] = useState([]);
  const wsRef = useRef(null);

  useEffect(() => {
    const connectWs = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);
      ws.onclose = () => {
        setConnected(false);
        setTimeout(connectWs, 3000);
      };

      ws.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'SESSION_INFO') {
            setSession({
              state: msg.state,
              track: msg.session?.circuit_short_name || 'Unknown',
              name: msg.session?.session_name || 'No Session'
            });
          } else if (msg.type === 'TELEMETRY') {
            const snap = msg.snapshot;
            if (snap && snap.grid) {
              setGrid(snap.grid);
              setMeta(snap.meta || {});
            }
          } else if (msg.type === 'INSIGHT') {
            setTriggers(prev => [msg.trigger, ...prev].slice(0, 50));
          } else if (msg.type === 'RACE_CONTROL') {
            // Can be handled via generic trigger or we just read meta.sc_active
          }
        } catch (err) {
          console.error('WS Parse Error', err);
        }
      };
    };

    connectWs();
    return () => {
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  const drivers = Object.values(grid).sort((a, b) => {
    const posA = a.position > 0 ? a.position : 99;
    const posB = b.position > 0 ? b.position : 99;
    return posA - posB;
  });

  const isLive = session.state === 'LIVE';
  const showScBanner = meta.sc_active || meta.vsc_active;
  const bannerType = meta.sc_active ? 'SAFETY CAR' : 'VIRTUAL SAFETY CAR';

  return (
    <div className="app-container">
      <header className="topbar">
        <div className="topbar-left">
          <div className="f1-logo-wrap">
            <div className="f1-emblem">F1</div>
            <div className="brand-text">
              <div className="brand-title">Live Insights</div>
              <div className="brand-sub">Strategy AI</div>
            </div>
          </div>
          <div className="topbar-divider"></div>
          <div className="race-badge">
            <div className={`live-dot ${isLive ? '' : 'disconnected'}`}></div>
            {session.track} - {session.name}
          </div>
        </div>
        <div className="topbar-right">
          <div className="lap-counter">
            <div className="lap-label">Current Lap</div>
            <div className="lap-value">{meta.lap || '--'}</div>
          </div>
          <div className={`conn-status ${connected ? 'connected' : 'error'}`}>
            {connected ? 'CONNECTED' : 'RECONNECTING...'}
          </div>
        </div>
      </header>

      {isLive ? (
        <main className="main-grid">
          {showScBanner && (
            <div className={`race-control-banner ${meta.sc_active ? 'sc' : ''}`}>
              <span>⚠️ {bannerType} DEPLOYED</span>
              <span>NO OVERTAKING</span>
            </div>
          )}

          <section className="tower-container card">
            <div className="card-header">
              <div className="card-title">Live Leaderboard</div>
            </div>
            <div className="tower-list">
              {drivers.map(d => (
                <div key={d.code} className="driver-row" style={{'--team-color': `#${d.team_colour_hex}`}}>
                  <div className="driver-pos">{d.position > 0 ? d.position : '-'}</div>
                  <div className="driver-code">{d.code}</div>
                  <div className="driver-gap">{formatGap(d.gap_to_leader_s)}</div>
                  <div className="driver-lap">{formatLapTime(d.lap_time_s)}</div>
                  <div className="tyre-info">
                    <span className={`tyre-dot tyre-${d.tyre_compound?.[0] || 'UNKNOWN'}`}></span>
                    {d.tyre_age_laps || 0}L
                  </div>
                </div>
              ))}
            </div>
          </section>

          <section className="track-map-container card">
            {/* Raw GPS points mapped to SVG. OpenF1 coordinates vary widely, we use a dynamic viewBox based on min/max of current data as a hack, or a fixed large viewbox */}
            <svg className="svg-map" viewBox="-10000 -10000 20000 20000" preserveAspectRatio="xMidYMid meet">
              <g transform="scale(1, -1)"> {/* Flip Y axis for standard top-down map */}
                {drivers.map(d => {
                  if (!d.x_pos || !d.y_pos) return null;
                  return (
                    <circle 
                      key={d.code}
                      className="car-dot"
                      cx={d.x_pos} 
                      cy={d.y_pos} 
                      r="400" 
                      fill={`#${d.team_colour_hex}`} 
                      stroke="#fff" 
                      strokeWidth="50" 
                    />
                  );
                })}
              </g>
            </svg>
          </section>

          <aside className="sidebar">
            <div className="card" style={{ flex: 1, display: 'flex', flexDirection: 'column' }}>
              <div className="card-header">
                <div className="card-title">⚡ Trigger Events</div>
                <div className="card-badge" style={{background: 'var(--f1-red-dim)', color: 'var(--f1-red)'}}>
                  {triggers.length} FIRED
                </div>
              </div>
              <div className="trigger-list">
                {triggers.length === 0 ? (
                  <div style={{padding: '20px', textAlign: 'center', color: 'var(--text-muted)'}}>
                    Monitoring telemetry...
                  </div>
                ) : (
                  triggers.map((t, idx) => (
                    <div key={idx} className={`trigger-item ${t.trigger.toLowerCase()}`}>
                      <div className="trigger-top">
                        <div className="trigger-badge">{t.trigger}</div>
                        <div style={{fontSize: '9px', color: 'var(--text-muted)'}}>Lap {t.lap}</div>
                      </div>
                      <div className="trigger-desc">{t.driver_code}: {t.reason}</div>
                    </div>
                  ))
                )}
              </div>
            </div>
          </aside>
        </main>
      ) : (
        <div className="idle-state">
          <h2>No Active Session</h2>
          <p>Waiting for the next Formula 1 session to begin...</p>
        </div>
      )}
    </div>
  );
}
