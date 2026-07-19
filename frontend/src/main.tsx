import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import { mapTimelineFrameToPlayback, TimelinePlaybackSample } from './playback';
import { displayEndFrame, markerDisplayEnd, markerIsActive, markerPositionPercent, markerSeekFrame, TimelineMarker } from './markers';

type FrameRate = { numerator: number; denominator: number };
type Asset = { id: string; path: string; name: string; codec: string; width: number; height: number; fps: FrameRate; duration_frames: number };
type Clip = { id: string; asset_id: string; source_in_frame: number; source_out_frame: number; timeline_start_frame: number };
type Track = { id: string; name: string; clips: Clip[] };
type Timeline = { id: string; name: string; external_refs: unknown[]; tracks: Track[]; markers: TimelineMarker[] };
type Project = { schema_version: number; id: string; name: string; revision: number; revision_id: string; external_refs: unknown[]; fps: FrameRate | null; assets: Asset[]; timeline: Timeline };
type TrimEdge = 'in' | 'out';
type TrimPreview = { clipId: string; sourceInFrame: number; sourceOutFrame: number };
type MovePreview = { clipId: string; timelineStartFrame: number };
type RenderState = 'idle' | 'rendering' | 'success' | 'failure';
type RenderedMedia = { url: string; revision: number };
type PreviewMode = 'live' | 'rendered';
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
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [uploadBusy, setUploadBusy] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [error, setError] = useState('');
  const [selectedClipId, setSelectedClipId] = useState<string | null>(null);
  const [selectedMarkerId, setSelectedMarkerId] = useState<string | null>(null);
  const [markerEditorOpen, setMarkerEditorOpen] = useState(false);
  const [editingMarkerId, setEditingMarkerId] = useState<string | null>(null);
  const [markerDraft, setMarkerDraftState] = useState({ name: '', type: 'generic', description: '', range: false, endFrame: '' });
  const setMarkerDraft = (next: typeof markerDraft) => {
    setMarkerDraftState(next);
    if (selectedMarker && next.name === selectedMarker.name && next.type === selectedMarker.type) {
      setEditingMarkerId(selectedMarker.id);
      setMarkerEditorOpen(true);
    }
    return false;
  };
  const [frame, setFrame] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [trimPreview, setTrimPreview] = useState<TrimPreview | null>(null);
  const trimGesture = useRef<{ clipId: string; edge: TrimEdge; startX: number; initialIn: number; initialOut: number; width: number } | null>(null);
  const trimPreviewRef = useRef<TrimPreview | null>(null);
  const [movePreview, setMovePreview] = useState<MovePreview | null>(null);
  const moveGesture = useRef<{ clipId: string; startX: number; initialStart: number; width: number } | null>(null);
  const movePreviewRef = useRef<MovePreview | null>(null);
  const deferredExternalProject = useRef<Project | null>(null);
  const [renderState, setRenderState] = useState<RenderState>('idle');
  const [renderedPreview, setRenderedPreview] = useState<RenderedMedia | null>(null);
  const [previewMode, setPreviewMode] = useState<PreviewMode>('live');
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
  const fileInput = useRef<HTMLInputElement>(null);
  const playingRef = useRef(false);
  const playbackClock = useRef<{ startTime: number; startFrame: number; raf: number } | null>(null);
  const videoSync = useRef<{ clipId: string | null; assetId: string | null }>({ clipId: null, assetId: null });
  const asset = project?.assets[0];
  const clips = project?.timeline.tracks[0]?.clips ?? [];
  const selectedClip = clips.find((clip) => clip.id === selectedClipId) ?? null;
  const fps = fpsValue(project);
  const timelineDuration = Math.max(1, ...clips.map((clip) => clip.timeline_start_frame + clipDuration(clip)));
  const displayTimelineDuration = Math.max(1, displayEndFrame(timelineDuration, project?.timeline.markers ?? []));
  const selectedMarker = project?.timeline.markers.find((marker) => marker.id === selectedMarkerId) ?? null;
  const playbackSample: TimelinePlaybackSample = project?.fps
    ? mapTimelineFrameToPlayback(clips, frame, project.fps)
    : { kind: 'gap', timeline_frame: frame };
  const playheadInsideSelected = Boolean(selectedClip && frame > selectedClip.timeline_start_frame && frame < selectedClip.timeline_start_frame + clipDuration(selectedClip));
  const editHint = !selectedClip
    ? 'Select a clip to edit.'
    : !playheadInsideSelected
      ? 'Move the playhead inside the selected clip.'
      : 'Ready for an edit at the playhead.';

  useEffect(() => {
    void api<Health>('/health').then(setHealth).catch(() => setHealth(null));
  }, []);

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

  useEffect(() => {
    const preventNavigation = (event: DragEvent) => event.preventDefault();
    window.addEventListener('dragover', preventNavigation);
    window.addEventListener('drop', preventNavigation);
    return () => {
      window.removeEventListener('dragover', preventNavigation);
      window.removeEventListener('drop', preventNavigation);
    };
  }, []);

  const refresh = (next: Project, selection: string | null = selectedClipId, markerSelection: string | null = selectedMarkerId) => {
    stopTimelinePlayback();
    setProject(next);
    setSelectedClipId(selection && next.timeline.tracks[0]?.clips.some((clip) => clip.id === selection) ? selection : null);
    setSelectedMarkerId(markerSelection && next.timeline.markers.some((marker) => marker.id === markerSelection) ? markerSelection : null);
    if (markerSelection && !next.timeline.markers.some((marker) => marker.id === markerSelection)) setMarkerEditorOpen(false);
    setRenderedPreview(null);
    setPreviewMode('live');
    setExportedMedia(null);
    setRenderState('idle');
    setSilenceRemoval(null);
    setError('');
  };
  const sampleAt = (timelineFrame: number): TimelinePlaybackSample => project?.fps
    ? mapTimelineFrameToPlayback(clips, timelineFrame, project.fps)
    : { kind: 'gap', timeline_frame: timelineFrame };
  const syncVideoToTimeline = (timelineFrame: number, force = false) => {
    const element = video.current;
    const currentRenderedPreview = renderedPreview?.revision === project?.revision ? renderedPreview : null;
    if (element && previewMode === 'rendered' && currentRenderedPreview) {
      element.controls = false;
      element.style.visibility = 'visible';
      const renderedTime = project?.fps ? timelineFrame * project.fps.denominator / project.fps.numerator : 0;
      const sourceChanged = element.getAttribute('src') !== currentRenderedPreview.url;
      if (sourceChanged) {
        element.src = currentRenderedPreview.url;
        element.load();
        element.onloadedmetadata = () => {
          if (video.current === element) {
            element.currentTime = renderedTime;
            if (playingRef.current) void element.play().catch(() => undefined);
          }
        };
      }
      if (force || sourceChanged || Math.abs(element.currentTime - renderedTime) > 0.12) element.currentTime = renderedTime;
      if (playingRef.current && element.paused) void element.play().catch(() => undefined);
      return;
    }
    const sample = sampleAt(timelineFrame);
    if (!element || sample.kind === 'gap') {
      element?.pause();
      if (element) { element.controls = false; element.style.visibility = 'hidden'; }
      return;
    }
    element.controls = false;
    element.style.visibility = 'visible';
    const sourceUrl = `/api/projects/${project?.id}/assets/${sample.clip.asset_id}/media`;
    const sourceChanged = element.getAttribute('src') !== sourceUrl;
    if (sourceChanged) {
      element.src = sourceUrl;
      element.load();
      element.onloadedmetadata = () => {
        if (video.current === element) {
          element.currentTime = sample.source_time_seconds;
          if (playingRef.current) void element.play().catch(() => undefined);
        }
      };
    }
    const assetChanged = videoSync.current.assetId !== sample.clip.asset_id;
    const clipChanged = videoSync.current.clipId !== sample.clip.id;
    videoSync.current = { clipId: sample.clip.id, assetId: sample.clip.asset_id };
    if (force || sourceChanged || assetChanged || clipChanged || Math.abs(element.currentTime - sample.source_time_seconds) > 0.12) {
      element.currentTime = sample.source_time_seconds;
    }
    if (playingRef.current && element.paused) void element.play().catch(() => undefined);
  };
  const stopTimelinePlayback = () => {
    if (playbackClock.current) cancelAnimationFrame(playbackClock.current.raf);
    playbackClock.current = null;
    playingRef.current = false;
    setPlaying(false);
    video.current?.pause();
  };
  const tickTimelinePlayback = (now: number) => {
    const clock = playbackClock.current;
    if (!clock || !fps) return;
    const elapsedFrames = Math.floor((now - clock.startTime) * fps / 1000);
    const nextFrame = Math.min(timelineDuration, clock.startFrame + elapsedFrames);
    syncVideoToTimeline(nextFrame);
    setFrame(nextFrame);
    if (nextFrame >= timelineDuration) {
      playbackClock.current = null;
      playingRef.current = false;
      setPlaying(false);
      video.current?.pause();
      return;
    }
    clock.raf = requestAnimationFrame(tickTimelinePlayback);
  };
  const playTimeline = () => {
    if (!project || !fps || !clips.length) return;
    const startFrame = frame >= timelineDuration ? 0 : frame;
    playingRef.current = true;
    setPlaying(true);
    playbackClock.current = { startTime: performance.now(), startFrame, raf: 0 };
    syncVideoToTimeline(startFrame, true);
    setFrame(startFrame);
    playbackClock.current.raf = requestAnimationFrame(tickTimelinePlayback);
  };
  const toggleTimelinePlayback = () => {
    if (playingRef.current) stopTimelinePlayback();
    else playTimeline();
  };
  const seek = (nextFrame: number) => {
    const target = Math.max(0, Math.min(displayTimelineDuration, Math.round(nextFrame)));
    if (playingRef.current && playbackClock.current) {
      playbackClock.current.startTime = performance.now();
      playbackClock.current.startFrame = target;
    }
    syncVideoToTimeline(target, true);
    setFrame(target);
  };
  const create = async () => {
    try {
      const next = await api<Project>('/projects', { method: 'POST', body: JSON.stringify({ name: 'Untitled project' }) });
      refresh(next, null);
      return next;
    } catch (exception) { setError(String(exception)); return null; }
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
  const chooseFile = () => fileInput.current?.click();
  const uploadFile = async (file: File | undefined, multiple = false) => {
    if (!file || uploadBusy) return;
    if (multiple) { setError('Please choose one video at a time.'); return; }
    if (!file.type.startsWith('video/') && !/\.(mp4|mov|m4v|webm|mkv)$/i.test(file.name)) {
      setError('Please choose a supported video file.');
      return;
    }
    setSelectedFile(file);
    setUploadBusy(true);
    setError('');
    try {
      const target = project ?? await create();
      if (!target) return;
      const form = new FormData();
      form.append('file', file, file.name);
      form.append('expected_revision', String(target.revision));
      const response = await fetch(`/api/projects/${target.id}/assets/upload`, { method: 'POST', body: form });
      if (!response.ok) throw new Error(await response.text());
      refresh(await response.json() as Project, null);
    } catch (exception) { setError(String(exception)); }
    finally { setUploadBusy(false); }
  };
  const handleDrop = (event: React.DragEvent<HTMLElement>) => {
    event.preventDefault();
    setDragOver(false);
    void uploadFile(event.dataTransfer.files[0], event.dataTransfer.files.length > 1);
  };
  const mutate = async (route: string, body: Record<string, unknown>, selection: string | null = selectedClipId) => {
    if (!project) return;
    try { refresh(await api<Project>(`/projects/${project.id}/clips/${route}`, { method: 'POST', body: JSON.stringify({ ...body, expected_revision: project.revision }) }), selection); }
    catch (exception) { setError(String(exception)); }
  };
  const mutateMarker = async (route: 'add' | 'update' | 'delete', body: Record<string, unknown>, markerSelection: string | null = selectedMarkerId) => {
    if (!project) return;
    try {
      const response = await api<Project & { marker_id?: string; deleted_marker_id?: string }>(`/projects/${project.id}/markers/${route}`, {
        method: 'POST', body: JSON.stringify({ ...body, expected_revision: project.revision }),
      });
      const nextMarkerId = response.marker_id ?? markerSelection;
      refresh(response, null, nextMarkerId);
      if (route === 'delete') setMarkerEditorOpen(false);
    } catch (exception) { setError(String(exception)); }
  };
  const split = () => {
    if (!selectedClip) { setError('Select a clip to edit.'); return; }
    if (!playheadInsideSelected) { setError('Move the playhead inside the selected clip.'); return; }
    void mutate('split', { clip_id: selectedClip.id, timeline_frame: frame });
  };
  const trimStartToPlayhead = () => {
    if (!selectedClip) { setError('Select a clip to edit.'); return; }
    if (!playheadInsideSelected) { setError('Move the playhead inside the selected clip.'); return; }
    const framesIntoClip = frame - selectedClip.timeline_start_frame;
    void mutate('trim', { clip_id: selectedClip.id, source_in_frame: selectedClip.source_in_frame + framesIntoClip }, selectedClip.id);
  };
  const trimEndToPlayhead = () => {
    if (!selectedClip) { setError('Select a clip to edit.'); return; }
    if (!playheadInsideSelected) { setError('Move the playhead inside the selected clip.'); return; }
    const framesIntoClip = frame - selectedClip.timeline_start_frame;
    void mutate('trim', { clip_id: selectedClip.id, source_out_frame: selectedClip.source_in_frame + framesIntoClip }, selectedClip.id);
  };
  const remove = () => { if (selectedClip) void mutate('delete', { clip_id: selectedClip.id }, null); else setError('Select a clip to edit.'); };
  const openMarkerDraft = () => {
    setSelectedClipId(null);
    setSelectedMarkerId(null);
    setEditingMarkerId(null);
    setMarkerDraft({ name: '', type: 'generic', description: '', range: false, endFrame: '' });
    setMarkerEditorOpen(true);
  };
  const saveMarkerDraft = () => {
    let endFrame: number | null = null;
    if (markerDraft.range) endFrame = Number(markerDraft.endFrame);
    if (!markerDraft.name.trim()) { setError('Marker name is required.'); return; }
    if (markerDraft.range && (endFrame === null || !Number.isInteger(endFrame) || endFrame <= frame)) { setError('Range marker end must be an integer greater than its start.'); return; }
    if (editingMarkerId) {
      void mutateMarker('update', { marker_id: editingMarkerId, changes: { start_frame: selectedMarker?.start_frame ?? frame, end_frame: endFrame, name: markerDraft.name, type: markerDraft.type, description: markerDraft.description } }, editingMarkerId);
    } else {
      void mutateMarker('add', { start_frame: frame, end_frame: endFrame, name: markerDraft.name, type: markerDraft.type, description: markerDraft.description, production: { shot_ids: [], dialogue_line_ids: [], external_refs: [] } }, null);
    }
    setEditingMarkerId(null);
    setMarkerEditorOpen(false);
  };
  const openSelectedMarkerEditor = () => {
    if (!selectedMarker) return;
    setEditingMarkerId(selectedMarker.id);
    setMarkerDraft({ name: selectedMarker.name, type: selectedMarker.type, description: selectedMarker.description, range: selectedMarker.end_frame !== null, endFrame: selectedMarker.end_frame === null ? '' : String(selectedMarker.end_frame) });
    setMarkerEditorOpen(true);
  };
  const updateSelectedMarker = (changes: Record<string, unknown>) => {
    if (selectedMarker) void mutateMarker('update', { marker_id: selectedMarker.id, changes }, selectedMarker.id);
  };
  const moveSelectedMarkerToPlayhead = () => {
    if (!selectedMarker) return;
    const duration = selectedMarker.end_frame === null ? null : selectedMarker.end_frame - selectedMarker.start_frame;
    updateSelectedMarker({ start_frame: frame, end_frame: duration === null ? null : frame + duration });
  };
  const renderProject = async (kind: 'preview' | 'export') => {
    if (!project || renderState === 'rendering') return;
    setRenderState('rendering');
    setError('');
    try {
      const result = await api<{ url: string; revision: number }>(`/projects/${project.id}/${kind === 'preview' ? 'render-preview' : 'export'}`, { method: 'POST', body: JSON.stringify({ expected_revision: project.revision }) });
      const media = { url: `${result.url}?revision=${result.revision}`, revision: result.revision };
      if (kind === 'preview') {
        stopTimelinePlayback();
        setFrame(0);
        setRenderedPreview(media);
        setPreviewMode('rendered');
      }
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
          selected_clip_id: selectedClipId, playhead_frame: frame, visible_track_id: project.timeline.tracks[0]?.id ?? null,
          selected_marker_id: selectedMarkerId,
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
    const destination = Math.max(0, Math.round(((event.clientX - bounds.left) / bounds.width) * displayTimelineDuration));
    if (clipId) void mutate('move', { clip_id: clipId, timeline_start_frame: destination }, clipId);
  };
  useEffect(() => {
    if (!playingRef.current) syncVideoToTimeline(frame, true);
  }, [project?.id, project?.revision, frame, previewMode, renderedPreview?.url]);

  return <main>
    <header><h1>TextSequence</h1><span>v0.2.2 · MCP-native NLE</span></header>
    <section className="toolbar"><button onClick={create}>New project</button><button onClick={open}>Open latest</button><div className={`import-controls ${dragOver ? 'drag-over' : ''}`} onDragEnter={(event) => { event.preventDefault(); setDragOver(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}><button onClick={chooseFile} disabled={uploadBusy}>{uploadBusy ? 'Importing…' : 'Choose Video'}</button><input ref={fileInput} type="file" accept="video/*,.mp4,.mov,.m4v" hidden onChange={(event) => { void uploadFile(event.target.files?.[0]); event.currentTarget.value = ''; }} /><span>{selectedFile?.name ?? 'Drop a video here'}</span></div><button onClick={() => void renderProject('preview')} disabled={!project || renderState === 'rendering'}>{renderState === 'rendering' ? 'Rendering…' : 'Render Preview'}</button><button onClick={() => void renderProject('export')} disabled={!project || renderState === 'rendering'}>Export MP4</button></section>
    <section className="connections"><h2>Agent connections</h2><div className="connection-row"><div><strong>TextSequence MCP</strong><span className="status ready">● {health?.mcp.status === 'running' ? 'Running' : 'Checking'}</span><p>{health?.mcp.transport ?? 'Streamable HTTP'}</p><p><strong>Available tools: {health?.mcp.tool_count ?? 15}</strong></p><code>{health?.mcp.endpoint ?? 'http://127.0.0.1:8000/mcp'}</code></div><button onClick={() => void navigator.clipboard?.writeText(health?.mcp.endpoint ?? 'http://127.0.0.1:8000/mcp')}>Copy MCP URL</button></div><div className="connection-row"><div><strong>Built-in assistant</strong><span className={`status ${health?.built_in_assistant.configured ? 'ready' : 'optional'}`}>● {health?.built_in_assistant.configured ? 'Ready' : 'Optional · Not configured'}</span><p>{health?.built_in_assistant.configured ? 'OpenAI Agents SDK' : 'Optional for core editing. Connect an external MCP agent or configure OPENAI_API_KEY.'}</p></div></div></section>
    {error && <p className="error">{error}</p>}
    {project && <><div className="preview-mode" aria-label="Preview mode"><strong>PREVIEW</strong><button className={previewMode === 'live' ? 'active' : ''} onClick={() => setPreviewMode('live')}>Live</button><button className={previewMode === 'rendered' ? 'active' : ''} onClick={() => setPreviewMode('rendered')} disabled={!renderedPreview || renderedPreview.revision !== project.revision}>Rendered</button><span>{previewMode === 'rendered' ? 'Rendered MP4 · linear timeline playback' : 'Source media · clip mapping playback'}</span></div><section className="editing-toolbar" aria-label="Editing actions"><div className="editing-heading"><strong>EDIT</strong><span>{selectedClip ? `Selected clip · ${selectedClip.id.slice(0, 12)}` : selectedMarker ? `Selected marker · ${selectedMarker.name}` : 'No selection'} · Playhead frame {frame}</span></div><div className="editing-actions"><button onClick={toggleTimelinePlayback} disabled={!clips.length}>{playing ? 'Pause' : 'Play'}</button><button onClick={split} disabled={!selectedClip || !playheadInsideSelected} title={editHint}>Split at Playhead</button><button onClick={trimStartToPlayhead} disabled={!selectedClip || !playheadInsideSelected} title={editHint}>Trim Start to Playhead</button><button onClick={trimEndToPlayhead} disabled={!selectedClip || !playheadInsideSelected} title={editHint}>Trim End to Playhead</button><button onClick={remove} disabled={!selectedClip} title={selectedClip ? 'Delete the selected clip' : 'Select a clip to edit.'}>Delete Selected</button><button onClick={openMarkerDraft}>Add Marker at Playhead</button></div><small className="editing-hint">{editHint}{fps ? ` · ${(frame / fps).toFixed(2)}s` : ''}</small>{markerEditorOpen && <div className="marker-editor"><label>Name <input value={markerDraft.name} onChange={(event) => setMarkerDraft({ ...markerDraft, name: event.target.value })} autoFocus /></label><label>Type <input value={markerDraft.type} onChange={(event) => setMarkerDraft({ ...markerDraft, type: event.target.value })} /></label><label>Description <input value={markerDraft.description} onChange={(event) => setMarkerDraft({ ...markerDraft, description: event.target.value })} /></label><label><input type="checkbox" checked={markerDraft.range} onChange={(event) => setMarkerDraft({ ...markerDraft, range: event.target.checked })} /> Range</label>{markerDraft.range && <label>End frame <input type="number" value={markerDraft.endFrame} onChange={(event) => setMarkerDraft({ ...markerDraft, endFrame: event.target.value })} /></label>}<button onClick={saveMarkerDraft}>Save marker</button><button onClick={() => setMarkerEditorOpen(false)}>Cancel</button></div>}</section></>}
    {!project && <section className={`empty-state ${dragOver ? 'drag-over' : ''}`} onDragEnter={(event) => { event.preventDefault(); setDragOver(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}><div className="empty-icon">TS</div><h2>Drop a video here to get started</h2><p>Choose a local video or drop one from Finder. Imported files stay on this machine.</p><div className="empty-actions"><button onClick={chooseFile} disabled={uploadBusy}>{uploadBusy ? 'Importing…' : 'Choose Video'}</button><button onClick={open}>Open latest</button></div></section>}
    {project && <section className="auto-edit"><div className="section-heading"><div><h3>Auto Edit · Silence Removal</h3><p>Analyze locally with FFmpeg, then apply one revision-checked batch edit.</p></div><span className="tool-badge">Deterministic</span></div><div className="auto-edit-controls"><label>Minimum silence <input type="number" min="1" value={silenceMinimumMs} onChange={(event) => setSilenceMinimumMs(Number(event.target.value))} /> ms</label><label>Keep padding <input type="number" min="0" value={silencePaddingMs} onChange={(event) => setSilencePaddingMs(Number(event.target.value))} /> ms</label><button onClick={() => void analyzeSilence()} disabled={silenceBusy}>{silenceBusy ? 'Analyzing…' : 'Analyze Silence'}</button><button onClick={() => void removeSilence()} disabled={silenceBusy || !silenceAnalysis || silenceAnalysis.summary.detected_silences === 0}>{silenceBusy ? 'Working…' : 'Remove Silence'}</button></div>{silenceAnalysis && <div className="auto-edit-result"><strong>Analysis</strong><span>{silenceAnalysis.summary.detected_silences} detected range(s) · {silenceAnalysis.summary.total_silence_frames} frames · minimum {silenceAnalysis.minimum_silence_ms} ms · threshold {silenceAnalysis.noise_threshold_db} dB</span></div>}{silenceRemoval && <div className="auto-edit-result success"><strong>Removal complete</strong><span>{silenceRemoval.removed_silences} range(s) removed · {silenceRemoval.removed_duration_ms} ms ({silenceRemoval.removed_frames} frames) · revision {silenceRemoval.revision}</span></div>}</section>}
    {project && <><h2>{project.name}</h2><details className="advanced-import"><summary>Advanced: import by local path</summary><div><input value={path} onChange={(event) => setPath(event.target.value)} placeholder="/path/to/video.mp4" /><button onClick={importAsset} disabled={!path || uploadBusy}>Import Path</button><p>Path imports reference an external local file without copying it.</p></div></details>{!asset && <section className={`media-import-area ${dragOver ? 'drag-over' : ''}`} onDragEnter={(event) => { event.preventDefault(); setDragOver(true); }} onDragOver={(event) => event.preventDefault()} onDragLeave={() => setDragOver(false)} onDrop={handleDrop}><strong>Drop a video here</strong><span>or use Choose Video above · Imported files stay on this machine.</span></section>}<section className="workspace"><div className="preview"><div className="preview-label">{previewMode === 'rendered' && renderedPreview?.revision === project.revision ? 'RENDERED TIMELINE PREVIEW' : 'SOURCE PREVIEW'}</div>{asset ? <video ref={video} src={previewMode === 'rendered' && renderedPreview?.revision === project.revision ? renderedPreview.url : `/api/projects/${project.id}/assets/${asset.id}/media`} /> : <p>Import a local video to begin.</p>}{exportedMedia && <a className="export-link" href={exportedMedia.url} target="_blank" rel="noreferrer">Open exported MP4</a>}</div><aside><h3>Project JSON</h3><pre>{JSON.stringify(project, null, 2)}</pre></aside></section><section className="timeline"><div className="track-label marker-track-label">MARKERS</div><div className="marker-track" style={{ '--marker-duration': `${displayTimelineDuration}` } as React.CSSProperties}>{project.timeline.markers.map((marker) => <button key={marker.id} className={`timeline-marker ${marker.end_frame === null ? 'point' : 'range'} ${marker.id === selectedMarkerId ? 'selected' : ''} ${markerIsActive(marker, frame) ? 'active' : ''}`} onClick={(event) => { event.stopPropagation(); setSelectedMarkerId(marker.id); setSelectedClipId(null); setMarkerEditorOpen(false); seek(markerSeekFrame(marker)); }} style={{ left: `${markerPositionPercent(marker, displayTimelineDuration)}%`, width: `${marker.end_frame === null ? 3 : Math.max(1, (markerDisplayEnd(marker) - marker.start_frame) / displayTimelineDuration * 100)}%` }} title={`${marker.name} · frame ${marker.start_frame}`}>{marker.end_frame === null ? '●' : marker.name}</button>)}</div><div className="marker-controls">{selectedMarker && <><strong>{selectedMarker.name}</strong><button onClick={() => setMarkerDraft({ name: selectedMarker.name, type: selectedMarker.type, description: selectedMarker.description, range: selectedMarker.end_frame !== null, endFrame: selectedMarker.end_frame === null ? '' : String(selectedMarker.end_frame) }) || setMarkerEditorOpen(true)}>Edit marker</button><button onClick={moveSelectedMarkerToPlayhead}>Move to Playhead</button><button onClick={() => void mutateMarker('delete', { marker_id: selectedMarker.id }, selectedMarker.id)}>Delete marker</button></>}</div><div className="track-label">V1</div><div className="track" onClick={(event) => { const bounds = event.currentTarget.getBoundingClientRect(); seek(((event.clientX - bounds.left) / bounds.width) * displayTimelineDuration); }} onDragOver={(event) => event.preventDefault()} onDrop={dropClip}>{clips.map((clip) => { const trim = trimPreview?.clipId === clip.id ? trimPreview : null; const move = movePreview?.clipId === clip.id ? movePreview : null; const sourceIn = trim ? trim.sourceInFrame : clip.source_in_frame; const sourceOut = trim ? trim.sourceOutFrame : clip.source_out_frame; const start = move ? move.timelineStartFrame : clip.timeline_start_frame; return <div key={clip.id} className={`clip ${clip.id === selectedClipId ? 'selected' : ''}`} draggable={false} onMouseDown={(event) => beginMove(event, clip)} onClick={(event) => { event.stopPropagation(); const bounds = (event.currentTarget.parentElement as HTMLElement).getBoundingClientRect(); seek(((event.clientX - bounds.left) / bounds.width) * displayTimelineDuration); setSelectedClipId(clip.id); setSelectedMarkerId(null); setError(''); }} style={{ left: `${start / displayTimelineDuration * 100}%`, width: `${(sourceOut - sourceIn) / displayTimelineDuration * 100}%` }}><span className="trim-handle trim-handle-in" onMouseDown={(event) => beginTrim(event, clip, 'in')} /><span className="clip-label">{asset?.name}</span><span className="trim-handle trim-handle-out" onMouseDown={(event) => beginTrim(event, clip, 'out')} /></div>; })}<div className="playhead" style={{ left: `${frame / displayTimelineDuration * 100}%` }} /></div><div className="timeline-meta">Frame {frame} / {timelineDuration} · {project.fps ? `${project.fps.numerator}/${project.fps.denominator} fps` : 'No media'}{selectedClip ? ` · Selected ${selectedClip.id}` : selectedMarker ? ` · Marker ${selectedMarker.name}` : ''}</div></section><section className="agent-panel"><div className="agent-heading"><h3>Built-in assistant</h3><span>{chatSending ? 'Processing…' : health?.built_in_assistant.configured ? 'Optional OpenAI integration' : 'Not configured'}</span></div>{!health?.built_in_assistant.configured ? <p className="agent-empty">Built-in assistant not configured. Connect an external MCP agent to TextSequence, or set OPENAI_API_KEY to enable this optional assistant.</p> : <><div className="agent-messages">{chatMessages.length === 0 && <p className="agent-empty">Ask the assistant to inspect or edit the current timeline.</p>}{chatMessages.map((item, index) => <article key={`${item.role}-${index}`} className={`agent-message ${item.role}`}><strong>{item.role === 'user' ? 'You' : 'Assistant'}</strong><p>{item.text}</p>{item.actions?.map((action, actionIndex) => <div className="agent-action" key={`${action.tool}-${actionIndex}`}>✓ {action.summary}</div>)}</article>)}</div><div className="agent-input"><input value={chatInput} disabled={chatSending} placeholder="Ask: Split this here" onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') void sendChat(); }} /><button onClick={() => void sendChat()} disabled={chatSending || !chatInput.trim()}>Send</button></div></>}</section></>}
  </main>;
}

createRoot(document.getElementById('root')!).render(<React.StrictMode><App /></React.StrictMode>);
