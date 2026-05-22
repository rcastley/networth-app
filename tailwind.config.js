/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/templates/**/*.html",
    "./app/help.yaml",
  ],
  theme: {
    extend: {
      colors: {
        brand: {
           50: "#EFF6FF",
          100: "#DBEAFE",
          500: "#3B82F6",
          700: "#1D4ED8",
          900: "#1E3A8A",
        },
      },
    },
  },
  plugins: [],
};
