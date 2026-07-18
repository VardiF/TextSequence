import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';

type FrameRate = { numerator: number; denominator: number };
type Asset = { id: string; path: string; name: string; codec: string; width: number; height: number; fps: FrameRate; duration_frames: number };
type Clip = { id: string; asset_id: string; source_in_frame: number; source_out_frame: number; timeline_start_frame: number };
type Track = { id: string; name: string; clips: Clip[] };
type Project = { id: string; name: string; revision: number; fps: FrameRate | null; assets: Asset[]; tracks: Track[] };
type TrimEdge = 'in' | 'out';
type TrimPreview = { clipId: string; sourceInFrame: number; sourceOutFrame: number };
type MovePreview = { clipId: string; timelineStartFrame: number };
type RenderState = 'idle' | 'rendering' | 'success' | 'failure';
type RenderedMedia = { url: string; revision: number };
type ChatAction = { tool: string; summary: string; arguments?: Record<string, unknown> };
type ChatMessage = { role: 'user' | 'assistant'; text: string; actions?: ChatAction[] };
type Health = { mcp: { status: string; endpoint: string; transport: string; tool_count: number }; built_in_assistant: { configured: boolean } };
type SilenceAnalysis = { minimum_silence_ms: number; noise_threshold_db: number; summary: { detected_silences: number; total_silence_frames: number }; silences: Array<{ start_frame: number; end_frame: number; duration_frames: number }> };
type SilenceRemoval = { detected_silences: number; removed_silences: number; removed_frames: number; removed_duration_ms: number; revision: number };

const api = async <T,>(path: string, init?: RequestInit): Promise<T> => {
  const response = await fetch(`/api${path}`, { headers: { 'Content-Type': 'application/json' }, ...init });
  if (!response.ok) throw new Error(await response.text());
  return response.json() as Promise<T>;
};
const fpsValue = (project: Project | null) => project?.fps ? project.fps.numerator / project.fps.denominator : 0;
const clipDuration = (clip: Clip) => clip.source_out_frame - clip.source_in_frame;

function App() {
  const [project, setProject] = useState<Project | null>(null);
  const [path, setPath] = useState('');
  const [error, setError] = useState('');
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const [trimPreview, setTrimPreview] = useState<TrimPreview | null>(null);
  const trimGesture = useRef<{ clipId: string; edge: TrimEdge; startX: number; initialIn: number; initialOut: number; width: number } | null>(null);
  const trimPreviewRef = useRef<TrimPreview | null>(null);
  const [movePreview, setMovePreview] = useState<MovePreview | null>(null);
  const moveGesture = useRef<{ clipId: string; startX: number; initialStart: number; width: number } | null>(null);
  const movePreviewRef = useRef<MovePreview | null>(null);
  const deferredExternalProject = useRef<Project | null>(null);
  const [renderState, setRenderState] = useState<RenderState>('idle');
  const [renderedPreview, setRenderedPreview] = useState<RenderedMedia | null>(null);
  const [exportedMedia, setExportedMedia] = useState<RenderedMedia | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState('');
  const [chatSending, setChatSending] = useState(false);
  const [editorSessionId] = useState(() => {
    const key = 'textsequence.editor_session_id';
    const existing = window.sessionStorage.getItem(key);
    if (existing) return existing;
    const created = `editor_${crypto.randomUUID().replace(/-/g, '')}`;
    window.sessionStorage.setItem(key, created);
    return created;
  });
  const [health, setHealth] = useState<Health | null>(null);
  const [silenceMinimumMs, setSilenceMinimumMs] = useState(700);
  const [silencePaddingMs, setSilencePaddingMs] = useState(0);
  const [silenceAnalysis, setSilenceAnalysis] = useState<SilenceAnalysis | null>(null);
  const [silenceRemoval, setSilenceRemoval] = useState<SilenceRemoval | null>(null);
  const [silenceBusy, setSilenceBusy] = useState(false);
  const video = useRef<HTMLVideoElement>(null);
  const asset = project?.assets[0];
  const clips = project?.tracks[0]?.clips ?? [];
  const selectedClip = clips.find((clip) => clip.id === selectedClipId) ?? null;
  const fps = fpsValue(project);
  const timelineDuration = Math.max(1, asset?.duration_frames ?? 1, ...clips.map((clip) => clip.timeline_start_frame + clipDuration(clip)));

  useEffect(() => {
    void api<Health>('/health').then(setHealth).catch(() => setHealth(null));
  }, []);

  useEffect(() => {
    const element = video.current;
    if (!element || !fps) return undefined;
    const tick = () => setFrame(Math.max(0, Math.round(element.currentTime * fps)));
    element.addEventListener('timeupdate', tick);
    return () => element.removeEventListener('timeupdate', tick);
  }, [fps, project]);

  useEffect(() => {
    if (!project) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const latest = await api<Project>(`/projects/${project.id}`);
        if (!cancelled && latest.revision !== project.revision) {
          if (trimGesture.current || moveGesture.current) deferredExternalProject.current = latest;
          else refresh(latest);
        }
      } catch { /* the active project remains usable while a poll is unavailable */ }
      try {
        const current = await api<{ revision: number; url: string }>(`/projects/${project.id}/renders/current/preview`);
        if (current.revision === project.revision && !trimGesture.current && !moveGesture.current) {
          setRenderedPreview({ revision: current.revision, url: `${current.url}?revision=${current.revision}` });
        }
      } catch { /* no current render is a normal state */ }
    };
    const timer = window.setInterval(() => void poll(), 1000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [project?.id, project?.revision]);

  const refresh = (next: Project, selection: string | null = selectedClipId) => {
    setProject(next);
    setSelectedClipId(selection && next.tracks[0]?.clips.some((clip) => clip.id === selection) ? selection : null);
    setRenderedPreview(null);
    setExportedMedia(null);
    setRenderState('idle');
    setSilenceRemoval(null);
    setError('');
  };
  const seek = (nextFrame: number) => {
    const target = Math.max(0, Math.min(timelineDuration, Math.round(nextFrame)));
    if (video.current && fps) video.current.currentTime = target / fps;
    setFrame(target);
  };
  const create = async () => {
    try { refresh(await api<Project>('/projects', { method: 'POST', body: JSON.stringify({ name: 'Untitled project' }) }), null); }
    catch (exception) { setError(String(exception)); }
  };
  const open = async () => {
    try { const projects = await api<Project[]>('/projects'); if (projects[0]) refresh(await api<Project>(`/projects/${projects[0].id}`), null); else await create(); }
    catch (exception) { setError(String(exception)); }
  };
  const importAsset = async () => {
    if (!project || !path) return;
    try { refresh(await api<Project>(`/projects/${project.id}/assets`, { method: 'POST', body: JSON.stringify({ path }) }), null); }
    catch (exception) { setError(String(exception)); }
  };
  const mutate = async (route: string, body: Record<string, unknown>, selection: string | null = selectedClipId) => {
    if (!project) return;
    try { refresh(await api<Project>(`/projects/${project.id}/clips/${route}`, { method: 'POST', body: JSON.stringify({ ...body, expected_revision: project.revision }) }), selection); }
    catch (exception) { setError(String(exception)); }
  };
  const split = () => {
    if (selectedClip && frame > selectedClip.timeline_start_frame && frame < selectedClip.timeline_start_frame + clipDuration(selectedClip)) void mutate('split', { clip_id: selectedClip.id, timeline_frame: frame });
    else setError('Place the playhead inside the selected clip to split it.');
  };
  const remove = () => { if (selectedClip) void mutate('delete', { clip_id: selectedClip.id }, null); else setError('Select a clip to delete.'); };
  const renderProject = async (kind: 'preview' | 'export') => {
    if (!project || renderState === 'rendering') return;
    setRenderState('rendering');
    setError('');
    try {
      const result = await api<{ url: string; revision: number }>(`/projects/${project.id}/${kind === 'preview' ? 'render-preview' : 'export'}`, { method: 'POST', body: JSON.stringify({ expected_revision: project.revision }) });
      const media = { url: `${result.url}?revision=${result.revision}`, revision: result.revision };
      if (kind === 'preview') setRenderedPreview(media);
      else setExportedMedia(media);
      setRenderState('success');
    } catch (exception) {
      setRenderState('failure');
      setError(String(exception));
    }
  };
  const sendChat = async () => {
    const message = chatInput.trim();
    if (!message || chatSending) return;
    if (!project) { setError('Open a project before using the built-in agent.'); return; }
    setChatMessages((current) => [...current, { role: 'user', text: message }]);
    setChatInput('');
    setChatSending(true);
    try {
      const response = await api<{ message: string; actions: ChatAction[] }>('/agent/chat', {
        method: 'POST',
        body: JSON.stringify({ editor_session_id: editorSessionId, message, editor_context: {
          editor_session_id: editorSessionId, project_id: project.id, observed_revision: project.revision,
          selected_clip_id: selectedClipId, playhead_frame: frame, visible_track_id: project.tracks[0]?.id ?? null,
        } }),
      });
      setChatMessages((current) => [...current, { role: 'assistant', text: response.message, actions: response.actions ?? [] }]);
    } catch (exception) {
      setChatMessages((current) => [...current, { role: 'assistant', text: String(exception), actions: [] }]);
    } finally { setChatSending(false); }
  };
  const analyzeSilence = async () => {
    if (!project) return;
    setSilenceBusy(true);
    try { setSilenceRemoval(null); setSilenceAnalysis(await api<SilenceAnalysis>(`/projects/${project.id}/analyze-silence`, { method: 'POST', body: JSON.stringify({ minimum_silence_ms: silenceMinimumMs, noise_threshold_db: -35 }) })); }
    catch (exception) { setError(String(exception)); }
    finally { setSilenceBusy(false); }
  };
  const removeSilence = async () => {
    if (!project) return;
    setSilenceBusy(true);
    try {
      const result = await api<{ project: Project }>(`/projects/${project.id}/remove-silence`, { method: 'POST', body: JSON.stringify({ expected_revision: project.revision, minimum_silence_ms: silenceMinimumMs, noise_threshold_db: -35, keep_padding_ms: silencePaddingMs }) });
      refresh(result.project);
      setSilenceAnalysis(null);
      setSilenceRemoval(result as unknown as SilenceRemoval);
    } catch (exception) { setError(String(exception)); }
    finally { setSilenceBusy(false); }
  };
  const beginTrim = (event: React.MouseEvent<HTMLElement>, clip: Clip, edge: TrimEdge) => {
    event.stopPropagation();
    const track = event.currentTarget.parentElement?.parentElement;
    if (!track) return;
    trimGesture.current = { clipId: clip.id, edge, startX: event.clientX, initialIn: clip.source_in_frame, initialOut: clip.source_out_frame, width: track.getBoundingClientRect().width };
    setSelectedClipId(clip.id);
    event.preventDefault();
  };
  const updateTrimAt = (clientX: number) => {
    const gesture = trimGesture.current;
    if (!gesture) return;
    const delta = Math.round((clientX - gesture.startX) / gesture.width * timelineDuration);
    const raw = gesture.edge === 'in' ? gesture.initialIn + delta : gesture.initialOut + delta;
    const value = gesture.edge === 'in'
      ? Math.max(0, Math.min(gesture.initialOut - 1, raw))
      : Math.max(gesture.initialIn + 1, raw);
    const next = { clipId: gesture.clipId, sourceInFrame: gesture.edge === 'in' ? value : gesture.initialIn, sourceOutFrame: gesture.edge === 'out' ? value : gesture.initialOut };
    trimPreviewRef.current = next;
    setTrimPreview(next);
  };
  const finishTrim = () => {
    const gesture = trimGesture.current;
    const preview = trimPreviewRef.current;
    trimGesture.current = null;
    trimPreviewRef.current = null;
    setTrimPreview(null);
    if (!gesture || !preview) { if (deferredExternalProject.current) { refresh(deferredExternalProject.current); deferredExternalProject.current = null; } return; }
    const body = gesture.edge === 'in'
      ? { clip_id: gesture.clipId, source_in_frame: preview.sourceInFrame }
      : { clip_id: gesture.clipId, source_out_frame: preview.sourceOutFrame };
    void mutate('trim', body, gesture.clipId);
  };
  const beginMove = (event: React.MouseEvent<HTMLDivElement>, clip: Clip) => {
    if ((event.target as HTMLElement).classList.contains('trim-handle')) return;
    const track = event.currentTarget.parentElement;
    if (!track) return;
    moveGesture.current = { clipId: clip.id, startX: event.clientX, initialStart: clip.timeline_start_frame, width: track.getBoundingClientRect().width };
    setSelectedClipId(clip.id);
    event.preventDefault();
  };
  const updateMoveAt = (clientX: number) => {
    const gesture = moveGesture.current;
    if (!gesture) return;
    const destination = Math.max(0, Math.round(gesture.initialStart + ((clientX - gesture.startX) / gesture.width) * timelineDuration));
    const next = { clipId: gesture.clipId, timelineStartFrame: destination };
    movePreviewRef.current = next;
    setMovePreview(next);
  };
  const finishMove = () => {
    const gesture = moveGesture.current;
    const preview = movePreviewRef.current;
    moveGesture.current = null;
    movePreviewRef.current = null;
    setMovePreview(null);
    if (gesture && preview) void mutate('move', { clip_id: gesture.clipId, timeline_start_frame: preview.timelineStartFrame }, gesture.clipId);
    else if (deferredExternalProject.current) { refresh(deferredExternalProject.current); deferredExternalProject.current = null; }
  };
  useEffect(() => {
    const move = (event: MouseEvent) => { if (trimGesture.current) updateTrimAt(event.clientX); else updateMoveAt(event.clientX); };
    const up = () => { if (trimGesture.current) finishTrim(); else if (moveGesture.current) finishMove(); };
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    return () => { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); };
  });
  const dropClip = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const clipId = event.dataTransfer.getData('text/clip-id');
    const bounds = event.currentTarget.getBoundingClientRect();
    const destination = Math.max(0, Math.round(((event.clientX - bounds.left) / bounds.width) * timelineDuration));
    if (clipId) void mutate('move', { clip_id: clipId, timeline_start_frame: destination }, clipId);
  };

  return <main>
    <header><h1>TextSequence</h1><span>v0.1.2 · MCP-native NLE</span></header>
    <section className="toolbar"><button onClick={create}>New project</button><button onClick={open}>Open latest</button><input value={path} onChange={(event) => setPath(event.target.value)} placeholder="Absolute path to a video" /><button onClick={importAsset} disabled={!project || !path}>Import video</button><button onClick={split} disabled={!selectedClip}>Split</button><button onClick={remove} disabled={!selectedClip}>Delete</button><button onClick={() => void renderProject('preview')} disabled={!project || renderState === 'rendering'}>{renderState === 'rendering' ? 'Rendering…' : 'Render Preview'}</button><button onClick={() => void renderProject('export')} disabled={!project || renderState === 'rendering'}>Export MP4</button></section>
    <section className="connections"><h2>Agent connections</h2><div className="connection-row"><div><strong>TextSequence MCP</strong><span className="status ready">● {health?.mcp.status === 'running' ? 'Running' : 'Checking'}</span><p>{health?.mcp.transport ?? 'Streamable HTTP'}</p><p><strong>Available tools: {health?.mcp.tool_count ?? 11}</strong></p><code>{health?.mcp.endpoint ?? 'http://127.0.0.1:8000/mcp'}</code></div><button onClick={() => void navigator.clipboard?.writeText(health?.mcp.endpoint ?? 'http://127.0.0.1:8000/mcp')}>Copy MCP URL</button></div><div className="connection-row"><div><strong>Built-in assistant</strong><span className={`status ${health?.built_in_assistant.configured ? 'ready' : 'optional'}`}>● {health?.built_in_assistant.configured ? 'Ready' : 'Optional · Not configured'}</span><p>{health?.built_in_assistant.configured ? 'OpenAI Agents SDK' : 'Optional for core editing. Connect an external MCP agent or configure OPENAI_API_KEY.'}</p></div></div></section>
    {error && <p className="error">{error}</p>}
    {!project && <section className="empty-state"><div className="empty-icon">TS</div><h2>Start an MCP-native edit</h2><p>Create a project or open the latest one, then import a local CFR H.264/AAC video.</p><ol><li>Create or open a project</li><li>Import a video</li><li>Edit manually or connect an MCP agent</li><li>Render a preview</li><li>Export MP4</li></ol><div className="empty-actions"><button onClick={create}>Create project</button><button onClick={open}>Open latest</button></div></section>}
    {project && <section className="auto-edit"><div className="section-heading"><div><h3>Auto Edit · Silence Removal</h3><p>Analyze locally with FFmpeg, then apply one revision-checked batch edit.</p></div><span className="tool-badge">Deterministic</span></div><div className="auto-edit-controls"><label>Minimum silence <input type="number" min="1" value={silenceMinimumMs} onChange={(event) => setSilenceMinimumMs(Number(event.target.value))} /> ms</label><label>Keep padding <input type="number" min="0" value={silencePaddingMs} onChange={(event) => setSilencePaddingMs(Number(event.target.value))} /> ms</label><button onClick={() => void analyzeSilence()} disabled={silenceBusy}>{silenceBusy ? 'Analyzing…' : 'Analyze Silence'}</button><button onClick={() => void removeSilence()} disabled={silenceBusy || !silenceAnalysis || silenceAnalysis.summary.detected_silences === 0}>{silenceBusy ? 'Working…' : 'Remove Silence'}</button></div>{silenceAnalysis && <div className="auto-edit-result"><strong>Analysis</strong><span>{silenceAnalysis.summary.detected_silences} detected range(s) · {silenceAnalysis.summary.total_silence_frames} frames · minimum {silenceAnalysis.minimum_silence_ms} ms · threshold {silenceAnalysis.noise_threshold_db} dB</span></div>}{silenceRemoval && <div className="auto-edit-result success"><strong>Removal complete</strong><span>{silenceRemoval.removed_silences} range(s) removed · {silenceRemoval.removed_duration_ms} ms ({silenceRemoval.removed_frames} frames) · revision {silenceRemoval.revision}</span></div>}</section>}
    {project && <><h2>{project.name}</h2><section className="workspace"><div className="preview"><div className="preview-label">{renderedPreview?.revision === project.revision ? 'RENDERED TIMELINE PREVIEW' : 'SOURCE PREVIEW'}</div>{asset ? <video ref={video} src={renderedPreview?.revision === project.revision ? renderedPreview.url : `/api/projects/${project.id}/assets/${asset.id}/media`} controls /> : <p>Import a local video to begin.</p>}{exportedMedia && <a className="export-link" href={exportedMedia.url} target="_blank" rel="noreferrer">Open exported MP4</a>}</div><aside><h3>Project JSON</h3><pre>{JSON.stringify(project, null, 2)}</pre></aside></section><section className="timeline"><div className="track-label">V1</div><div className="track" onClick={(event) => { const bounds = event.currentTarget.getBoundingClientRect(); seek(((event.clientX - bounds.left) / bounds.width) * timelineDuration); }} onDragOver={(event) => event.preventDefault()} onDrop={dropClip}>{clips.map((clip) => { const trim = trimPreview?.clipId === clip.id ? trimPreview : null; const move = movePreview?.clipId === clip.id ? movePreview : null; const sourceIn = trim ? trim.sourceInFrame : clip.source_in_frame; const sourceOut = trim ? trim.sourceOutFrame : clip.source_out_frame; const start = move ? move.timelineStartFrame : clip.timeline_start_frame; return <div key={clip.id} className={`clip ${clip.id === selectedClipId ? 'selected' : ''}`} draggable={false} onMouseDown={(event) => beginMove(event, clip)} onClick={(event) => { event.stopPropagation(); const bounds = (event.currentTarget.parentElement as HTMLElement).getBoundingClientRect(); seek(((event.clientX - bounds.left) / bounds.width) * timelineDuration); setSelectedClipId(clip.id); setError(''); }} onDragStart={(event) => { setSelectedClipId(clip.id); event.dataTransfer.setData('text/clip-id', clip.id); }} style={{ left: `${start / timelineDuration * 100}%`, width: `${(sourceOut - sourceIn) / timelineDuration * 100}%` }}><span className="trim-handle trim-handle-in" onMouseDown={(event) => beginTrim(event, clip, 'in')} /><span className="clip-label">{asset?.name}</span><span className="trim-handle trim-handle-out" onMouseDown={(event) => beginTrim(event, clip, 'out')} /></div>; })}<div className="playhead" style={{ left: `${frame / timelineDuration * 100}%` }} /></div><div className="timeline-meta">Frame {frame} / {timelineDuration} · {project.fps ? `${project.fps.numerator}/${project.fps.denominator} fps` : 'No media'}{selectedClip ? ` · Selected ${selectedClip.id}` : ''}</div></section><section className="agent-panel"><div className="agent-heading"><h3>Built-in assistant</h3><span>{chatSending ? 'Processing…' : health?.built_in_assistant.configured ? 'Optional OpenAI integration' : 'Not configured'}</span></div>{!health?.built_in_assistant.configured ? <p className="agent-empty">Built-in assistant not configured. Connect an external MCP agent to TextSequence, or set OPENAI_API_KEY to enable this optional assistant.</p> : <><div className="agent-messages">{chatMessages.length === 0 && <p className="agent-empty">Ask the assistant to inspect or edit the current timeline.</p>}{chatMessages.map((item, index) => <article key={`${item.role}-${index}`} className={`agent-message ${item.role}`}><strong>{item.role === 'user' ? 'You' : 'Assistant'}</strong><p>{item.text}</p>{item.actions?.map((action, actionIndex) => <div className="agent-action" key={`${action.tool}-${actionIndex}`}>✓ {action.summary}</div>)}</article>)}</div><div className="agent-input"><input value={chatInput} disabled={chatSending} placeholder="Ask: Split this here" onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void sendChat(); }} /><button onClick={() => void sendChat()} disabled={chatSending || !chatInput.trim()}>Send</button></div></>}</section></>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
