import { Monitor, Moon, Sun, type LucideIcon } from "lucide-react";
import { useTheme } from "@/context/ThemeContext";
import type { Theme } from "@/types/api";

interface ThemeChoice {
  value: Theme;
  label: string;
  tagline: string;
  icon: LucideIcon;
}

const CHOICES: ThemeChoice[] = [
  { value: "light", label: "Light", tagline: "Warm paper base", icon: Sun },
  { value: "dark", label: "Dark", tagline: "Low-light comfort", icon: Moon },
  { value: "system", label: "System", tagline: "Follow your device", icon: Monitor },
];

export default function AppearancePanel({
  variant = "desktop",
}: {
  variant?: "desktop" | "phone";
}) {
  const { theme, resolved, setTheme } = useTheme();
  const wrapperClass = variant === "phone" ? "phone__section" : "panel";

  return (
    <section className={wrapperClass} aria-labelledby="appearance-heading">
      {variant === "phone" ? (
        <h2 className="section-title" id="appearance-heading">Appearance</h2>
      ) : (
        <header className="panel__head"><h2 id="appearance-heading">Appearance</h2></header>
      )}
      <p className="muted">
        Choose a theme. System follows your device's light/dark setting;
        currently resolved to <strong>{resolved}</strong>.
      </p>
      <fieldset className="theme-choices">
        <legend className="sr-only">Choose your theme</legend>
        {CHOICES.map((c) => {
          const selected = theme === c.value;
          const Icon = c.icon;
          return (
            <label
              key={c.value}
              className={"theme-choice" + (selected ? " theme-choice--selected" : "")}
            >
              <input
                type="radio"
                name="theme-preference"
                value={c.value}
                checked={selected}
                onChange={() => setTheme(c.value)}
                className="theme-choice__input"
              />
              <Icon size={18} aria-hidden="true" className="theme-choice__icon" />
              <div className="theme-choice__body">
                <strong>{c.label}</strong>
                <span className="muted">{c.tagline}</span>
              </div>
            </label>
          );
        })}
      </fieldset>
    </section>
  );
}
