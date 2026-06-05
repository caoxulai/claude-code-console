export function SkeletonLine({ width = '100%', height = '16px' }) {
  return (
    <div
      className="skeleton"
      style={{ width, height, marginBottom: '0.5em' }}
    />
  );
}

export function SkeletonCard() {
  return (
    <div
      className="skeleton"
      style={{ width: '100%', height: '80px' }}
    />
  );
}
