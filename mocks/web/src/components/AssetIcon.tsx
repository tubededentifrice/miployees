import {
  AlarmSmoke,
  Biohazard,
  BrushCleaning,
  Car,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Heater,
  Package,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sprout,
  Sun,
  ThermometerSun,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";

// §14 "Icons": data fields store a Lucide icon name (PascalCase)
// and the web resolves them through this whitelist. Unknown names
// fall back to the generic Package glyph so stale data never breaks
// the render.
const REGISTRY: Record<string, LucideIcon> = {
  AlarmSmoke,
  Biohazard,
  BrushCleaning,
  Car,
  CookingPot,
  Droplets,
  Fan,
  FireExtinguisher,
  Flame,
  Heater,
  Refrigerator,
  ShieldCheck,
  Snowflake,
  Sprout,
  Sun,
  ThermometerSun,
  Utensils,
  Waves,
  WavesLadder,
  WashingMachine,
  Wrench,
  Zap,
};

export function AssetIcon({
  name,
  size = 16,
  className,
}: {
  name: string | null | undefined;
  size?: number;
  className?: string;
}) {
  const Icon = (name && REGISTRY[name]) || Package;
  return (
    <span className={"asset-icon" + (className ? " " + className : "")} aria-hidden="true">
      <Icon size={size} strokeWidth={1.75} />
    </span>
  );
}
