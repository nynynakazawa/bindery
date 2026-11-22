import { createTheme } from "@mui/material/styles";

export const theme = createTheme({
  palette: {
    mode: "dark",
    background: { default: "#000000", paper: "#0B0B0D" },
    text: { primary: "#F5F5F7", secondary: "#A1A1A6" },
    primary: { main: "#FFB547", contrastText: "#0B0B0D" },
    success: { main: "#30D158" },
    warning: { main: "#FFD60A" },
    error: { main: "#FF453A" },
    divider: "rgba(255,255,255,0.08)",
  },
  shape: { borderRadius: 12 },
  typography: {
    fontFamily: '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Hiragino Sans", sans-serif',
    body1: { fontSize: "13px", lineHeight: 1.5 },
    body2: { fontSize: "11px", lineHeight: 1.45 },
    button: { fontSize: "12px", fontWeight: 600, textTransform: "none" },
  },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: { root: { borderRadius: 12, minHeight: 36, transition: "180ms ease-out" } },
    },
    MuiTextField: { defaultProps: { size: "small" } },
    MuiStepLabel: { styleOverrides: { label: { fontSize: "11px" } } },
  },
});
