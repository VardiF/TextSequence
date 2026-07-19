export type TimelineMarker = {
  id: string;
  start_frame: number;
  end_frame: number | null;
  name: string;
  description: string;
  type: string;
};

export const markerDisplayEnd = (marker: TimelineMarker) =>
  marker.end_frame ?? marker.start_frame + 1;

export const displayEndFrame = (contentEndFrame: number, markers: TimelineMarker[]) =>
  Math.max(contentEndFrame, ...markers.map(markerDisplayEnd), 0);

export const markerIsActive = (marker: TimelineMarker, playheadFrame: number) =>
  marker.end_frame === null
    ? playheadFrame === marker.start_frame
    : marker.start_frame <= playheadFrame && playheadFrame < marker.end_frame;

export const markerPositionPercent = (marker: TimelineMarker, displayEnd: number) =>
  displayEnd > 0 ? (marker.start_frame / displayEnd) * 100 : 0;

export const markerSeekFrame = (marker: TimelineMarker) => marker.start_frame;
