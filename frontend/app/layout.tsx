import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "个人日本股票研究与模拟交易系统",
  description: "历史研究与模拟交易数据中心",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
