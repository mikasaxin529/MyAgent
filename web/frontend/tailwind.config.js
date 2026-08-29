/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", '[data-theme="dark"]'],
  theme: {
    extend: {
      fontFamily: {
        mono: ["JetBrains Mono", "ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
        sans: ["Manrope", "PingFang SC", "Microsoft YaHei", "-apple-system", "sans-serif"],
        serif: ["Fraunces", "Georgia", "Songti SC", "serif"],
      },
    },
  },
  plugins: [],
};