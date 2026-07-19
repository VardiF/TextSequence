export type PlaybackFrameRate = { numerator: number; denominator: number };

export type PlaybackClip = {
  id: string;
  asset_id: string;
  source_in_frame: number;
  source_out_frame: number;
  timeline_start_frame: number;
};

export type TimelinePlaybackSample =
  | { kind: 'clip'; clip: PlaybackClip; source_frame: number; source_time_seconds: number }
  | { kind: 'gap'; timeline_frame: number };

export const getClipAtTimelineFrame = (clips: PlaybackClip[], timelineFrame: number): PlaybackClip | null =>
  [...clips]
    .sort((left, right) => left.timeline_start_frame - right.timeline_start_frame || left.id.localeCompare(right.id))
    .find((clip) => timelineFrame >= clip.timeline_start_frame
      && timelineFrame < clip.timeline_start_frame + (clip.source_out_frame - clip.source_in_frame)) ?? null;

export const timelineFrameToSourceFrame = (clip: PlaybackClip, timelineFrame: number): number =>
  clip.source_in_frame + (timelineFrame - clip.timeline_start_frame);

export const timelineFrameToSourceTime = (clip: PlaybackClip, timelineFrame: number, fps: PlaybackFrameRate): number =>
  timelineFrameToSourceFrame(clip, timelineFrame) * fps.denominator / fps.numerator;

export const mapTimelineFrameToPlayback = (
  clips: PlaybackClip[],
  timelineFrame: number,
  fps: PlaybackFrameRate,
): TimelinePlaybackSample => {
  const clip = getClipAtTimelineFrame(clips, timelineFrame);
  if (!clip) return { kind: 'gap', timeline_frame: timelineFrame };
  return {
    kind: 'clip',
    clip,
    source_frame: timelineFrameToSourceFrame(clip, timelineFrame),
    source_time_seconds: timelineFrameToSourceTime(clip, timelineFrame, fps),
  };
};
