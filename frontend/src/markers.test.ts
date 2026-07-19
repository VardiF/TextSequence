import { describe, expect, it } from 'vitest';
import { displayEndFrame, markerDisplayEnd, markerIsActive, markerPositionPercent, markerSeekFrame, TimelineMarker } from './markers';

const point: TimelineMarker = { id: 'marker_point', start_frame: 10, end_frame: null, name: 'Point', description: '', type: 'generic' };
const range: TimelineMarker = { id: 'marker_range', start_frame: 20, end_frame: 40, name: 'Range', description: '', type: 'shot' };

describe('timeline marker helpers', () => {
  it('maps point and range extents without making a point inclusive of a second frame', () => {
    expect(markerDisplayEnd(point)).toBe(11);
    expect(markerDisplayEnd(range)).toBe(40);
    expect(displayEndFrame(15, [point, range])).toBe(40);
  });

  it('uses exclusive range active state and exact point state', () => {
    expect(markerIsActive(point, 10)).toBe(true);
    expect(markerIsActive(point, 11)).toBe(false);
    expect(markerIsActive(range, 20)).toBe(true);
    expect(markerIsActive(range, 39)).toBe(true);
    expect(markerIsActive(range, 40)).toBe(false);
  });

  it('maps marker position and seek to the canonical start frame', () => {
    expect(markerPositionPercent(range, 100)).toBe(20);
    expect(markerSeekFrame(range)).toBe(20);
  });
});
