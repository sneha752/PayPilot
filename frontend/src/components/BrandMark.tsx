/** PayPilot logo mark: a compass-like "P" tile, drawn inline (no asset). */
export function BrandMark() {
  return (
    <div
      aria-hidden
      className="flex h-9 w-9 items-center justify-center rounded-xl bg-brand-600 shadow-sm"
    >
      <svg viewBox="0 0 24 24" className="h-5 w-5 text-white" fill="none">
        <path
          d="M12 3l3.5 7L19 12l-7 3.5L8.5 19 5 12l7-3.5L12 3z"
          fill="currentColor"
          fillOpacity="0.9"
        />
      </svg>
    </div>
  );
}
