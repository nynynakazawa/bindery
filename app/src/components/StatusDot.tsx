import { Box } from "@mui/material";

export function StatusDot({ color, pulse = false }: { color: string; pulse?: boolean }) {
  return (
    <Box
      component="span"
      sx={{
        width: 7,
        height: 7,
        flex: "0 0 auto",
        borderRadius: "50%",
        backgroundColor: color,
        boxShadow: `0 0 10px ${color}`,
        animation: pulse ? "bindery-pulse 1.8s ease-in-out infinite" : "none",
        "@keyframes bindery-pulse": {
          "0%, 100%": { opacity: 0.48, transform: "scale(0.9)" },
          "50%": { opacity: 1, transform: "scale(1.12)" },
        },
      }}
    />
  );
}
