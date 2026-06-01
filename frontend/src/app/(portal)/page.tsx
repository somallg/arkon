"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth";

export default function DashboardPage() {
  const { user } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!user) return;

    if (user.role === "admin") {
      router.replace("/admin/statistics");
    } else {
      router.replace("/wiki");
    }
  }, [user, router]);

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center p-6 text-center">
      {/* Sleek, Glassmorphic Loading Container */}
      <div className="relative flex flex-col items-center gap-6 rounded-2xl border border-white/[0.08] bg-black/20 p-12 backdrop-blur-xl shadow-2xl transition-all duration-300">
        
        {/* Harmonious HSL Gradient Decorative Glow */}
        <div className="absolute -inset-1 rounded-2xl bg-gradient-to-r from-primary/30 to-violet-500/30 opacity-30 blur-lg transition-all"></div>
        
        {/* Dynamic Micro-animated Spinner */}
        <div className="relative h-12 w-12">
          <div className="absolute inset-0 rounded-full border-2 border-muted/30"></div>
          <div className="absolute inset-0 rounded-full border-2 border-t-primary border-r-primary animate-spin" style={{ animationDuration: '0.8s' }}></div>
        </div>

        {/* Dynamic typography and micro-content */}
        <div className="space-y-2 z-10">
          <h2 className="text-lg font-semibold tracking-wide text-foreground animate-pulse">
            Chuyển hướng không gian làm việc
          </h2>
          <p className="text-xs text-muted-foreground max-w-[280px]">
            Đang tải dữ liệu và xác định không gian làm việc phù hợp cho tài khoản của bạn...
          </p>
        </div>
      </div>
    </div>
  );
}
