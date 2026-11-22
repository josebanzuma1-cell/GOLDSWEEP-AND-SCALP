//+------------------------------------------------------------------+
//| GoldSweepPanel.mq5  (v2)                                         |
//| Draws GoldSweep's main signals and its scalper on the gold chart.|
//|                                                                  |
//| Main strategy: Common\Files\GoldSweep\signals.json (rewritten by |
//| the Python bot after every M5 bar): a BUY/SELL label with its    |
//| score, the entry, stop-loss and TP1-TP3 lines, risk and reward   |
//| boxes and the swept level; a small corner box with the mode,     |
//| trading state, pressure estimate and news bias.                  |
//|                                                                  |
//| Scalper (v2): Common\Files\GoldSweep\scalp.json (rewritten every |
//| minute): the HFT step-trail line, sweep "x" markers, each recent |
//| scalp's box with SL / ENTRY / TP1-TP3 labels, TP-hit markers and |
//| its result, and the scalper's dashboard box (mode, status,       |
//| market, the active scalp, today's and 30 days' results). It says|
//| STALE when the file stops updating.                              |
//|                                                                  |
//| It never trades. Its only output is Common\Files\GoldSweep\      |
//| scalp_commands.json: the Pause / Resume / Scalper off buttons    |
//| write one command line there, which the bot carries out like a   |
//| dashboard command (D99).                                         |
//+------------------------------------------------------------------+
#property copyright   "GoldSweep"
#property version     "2.00"
#property description "GoldSweep: main signals and the scalper (step line, scalp boxes, dashboard)."
#property description "Never trades. Reads Common\\Files\\GoldSweep\\signals.json and scalp.json."
#property indicator_chart_window
#property indicator_plots 0

input string InpFile         = "GoldSweep\\signals.json";        // Main signals (Common\Files)
input string InpScalpFile    = "GoldSweep\\scalp.json";          // Scalper picture (Common\Files)
input string InpCommandFile  = "GoldSweep\\scalp_commands.json"; // Button commands (Common\Files)
input int    InpRefreshSec   = 5;               // Read the files every N seconds
input int    InpStaleSec     = 180;             // Scalper file older than this: STALE
input bool   InpShowSignals  = true;            // Main strategy: signals and boxes
input bool   InpShowPanel    = true;            // Main strategy: corner box
input bool   InpShowScalper  = true;            // Scalper: boxes, tags, TP hits
input bool   InpShowStepLine = true;            // Scalper: HFT step-trail line
input bool   InpShowSweeps   = true;            // Scalper: sweep "x" markers
input bool   InpShowDashboard= true;            // Scalper: dashboard box and buttons
input int    InpBoxBars      = 24;              // Main boxes: width in bars of this chart
input int    InpScalpBoxBars = 40;              // Open scalp boxes: width in bars
input int    InpDashboardY   = 92;              // Dashboard: pixels above the bottom
input color  InpBuyColor     = C'46,204,113';   // BUY (#2ECC71)
input color  InpSellColor    = C'231,76,60';    // SELL (#E74C3C)
input color  InpStopColor    = C'231,76,60';    // Stop-loss line
input color  InpTargetColor  = C'154,209,168';  // Take-profit lines (#9AD1A8)
input color  InpGold         = C'212,175,55';   // GoldSweep gold (#D4AF37)
input color  InpPoolColor    = C'212,175,55';   // Swept liquidity level
input color  InpStepUp       = C'143,163,184';  // Step line, trend up (#8FA3B8)
input color  InpStepDown     = C'184,143,143';  // Step line, trend down
input color  InpRiskFill     = C'70,28,28';     // Risk zone
input color  InpRewardFill   = C'22,60,38';     // Reward zone
input color  InpPanelBack    = C'14,17,22';     // Dashboard background (#0E1116)

#define PREFIX  "GS_"
#define SPREFIX "GSS_"
#define BUTTON_PAUSE  "GSS_btn_pause"
#define BUTTON_OFF    "GSS_btn_off"

datetime g_modified       = 0;
long     g_size           = -1;
datetime g_scalp_modified = 0;
long     g_scalp_size     = -1;
string   g_state          = "";
long     g_updated_gmt    = 0;
int      g_clicks         = 0;

//+------------------------------------------------------------------+
int OnInit()
  {
   EventSetTimer(MathMax(1, InpRefreshSec));
   Refresh(true);
   return(INIT_SUCCEEDED);
  }

//+------------------------------------------------------------------+
void OnDeinit(const int reason)
  {
   EventKillTimer();
   ObjectsDeleteAll(0, PREFIX);
   ObjectsDeleteAll(0, SPREFIX);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const int begin,
                const double &price[])
  {
   return(rates_total);
  }

//+------------------------------------------------------------------+
void OnTimer()
  {
   Refresh(false);
   ShowStale();
  }

//+------------------------------------------------------------------+
//| The buttons: one command line for the bot, nothing else          |
//+------------------------------------------------------------------+
void OnChartEvent(const int id, const long &lparam, const double &dparam, const string &sparam)
  {
   if(id != CHARTEVENT_OBJECT_CLICK)
      return;
   string command = "";
   if(sparam == BUTTON_PAUSE)
      command = (g_state == "active") ? "pause" : "resume";
   else
      if(sparam == BUTTON_OFF)
         command = "scalper:off";
   if(command == "")
      return;
   ObjectSetInteger(0, sparam, OBJPROP_STATE, false);
   if(SendCommand(command))
      ObjectSetString(0, sparam, OBJPROP_TEXT, "sent");
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
bool SendCommand(const string command)
  {
   int handle = FileOpen(InpCommandFile, FILE_WRITE | FILE_TXT | FILE_ANSI | FILE_COMMON);
   if(handle == INVALID_HANDLE)
     {
      Print("GoldSweep panel: could not write ", InpCommandFile, " (error ", GetLastError(), ")");
      return(false);
     }
   g_clicks++;
   long stamp = (long)TimeGMT();
   long id = stamp * 100 + (g_clicks % 100);
   FileWriteString(handle, "{\"id\":" + IntegerToString(id) + ",\"t\":" + IntegerToString(stamp) +
                   ",\"cmd\":\"" + command + "\"}\r\n");
   FileClose(handle);
   return(true);
  }

//+------------------------------------------------------------------+
//| Value of "key":"..." in one line of the file                     |
//+------------------------------------------------------------------+
string JsonString(const string line, const string key)
  {
   string pattern = "\"" + key + "\":\"";
   int start = StringFind(line, pattern);
   if(start < 0)
      return("");
   start += StringLen(pattern);
   int stop = StringFind(line, "\"", start);
   if(stop < 0)
      return("");
   return(StringSubstr(line, start, stop - start));
  }

//+------------------------------------------------------------------+
//| Value of "key":number in one line of the file (0 when absent)    |
//+------------------------------------------------------------------+
double JsonNumber(const string line, const string key)
  {
   string pattern = "\"" + key + "\":";
   int start = StringFind(line, pattern);
   if(start < 0)
      return(0.0);
   start += StringLen(pattern);
   int stop = start;
   int length = StringLen(line);
   while(stop < length)
     {
      ushort c = StringGetCharacter(line, stop);
      if(c == ',' || c == '}' || c == ']')
         break;
      stop++;
     }
   return(StringToDouble(StringSubstr(line, start, stop - start)));
  }

//+------------------------------------------------------------------+
bool IsGoldChart()
  {
   string name = _Symbol;
   StringToUpper(name);
   return(StringFind(name, "XAU") >= 0 || StringFind(name, "GOLD") >= 0);
  }

//+------------------------------------------------------------------+
//| Read a file's lines when it changed; false when unchanged/absent |
//+------------------------------------------------------------------+
bool ReadLines(const string file, datetime &modified_seen, long &size_seen, const bool force,
               string &lines[], int &count, bool &missing)
  {
   count = 0;
   missing = false;
   int handle = FileOpen(file, FILE_READ | FILE_TXT | FILE_ANSI | FILE_COMMON |
                         FILE_SHARE_READ | FILE_SHARE_WRITE);
   if(handle == INVALID_HANDLE)
     {
      missing = true;
      return(false);
     }
   datetime modified = (datetime)FileGetInteger(handle, FILE_MODIFY_DATE);
   long size = (long)FileSize(handle);
   if(!force && modified == modified_seen && size == size_seen)
     {
      FileClose(handle);
      return(false);
     }
   modified_seen = modified;
   size_seen = size;
   while(!FileIsEnding(handle))
     {
      string line = FileReadString(handle);
      if(StringLen(line) == 0)
         continue;
      ArrayResize(lines, count + 1);
      lines[count] = line;
      count++;
     }
   FileClose(handle);
   return(true);
  }

//+------------------------------------------------------------------+
void Refresh(const bool force)
  {
   if(!IsGoldChart())
     {
      ShowStatus("GoldSweep panel: attach it to the gold chart (XAUUSD / GOLD).", clrSilver);
      return;
     }
   string lines[];
   int count = 0;
   bool missing = false;
   if(ReadLines(InpFile, g_modified, g_size, force, lines, count, missing))
      Draw(lines, count);
   else
      if(missing && force)
         ShowStatus("GoldSweep panel: waiting for " + InpFile + " (start GoldSweep).", clrSilver);
   string scalp[];
   int scalp_count = 0;
   bool scalp_missing = false;
   if(ReadLines(InpScalpFile, g_scalp_modified, g_scalp_size, force, scalp, scalp_count,
                scalp_missing))
      DrawScalper(scalp, scalp_count);
  }

//+------------------------------------------------------------------+
//| The main strategy (v1)                                           |
//+------------------------------------------------------------------+
void Draw(string &lines[], const int count)
  {
   ObjectsDeleteAll(0, PREFIX);
   if(count == 0)
     {
      ChartRedraw(0);
      return;
     }
   string head = lines[0];
   int shown = 0;
   for(int i = 1; i < count; i++)
     {
      if(StringFind(lines[i], "\"id\":\"") < 0)
         continue;
      if(InpShowSignals)
         DrawSignal(lines[i]);
      shown++;
     }
   if(InpShowPanel)
     {
      ShowLine(0, "GoldSweep " + JsonString(head, "mode") + " | trading " +
               JsonString(head, "state"), clrWhite);
      ShowLine(1, "Pressure: " + JsonString(head, "pressure") + " (estimate)", clrSilver);
      ShowLine(2, "News bias: " + DoubleToString(JsonNumber(head, "news_bias"), 2) + " | " +
               IntegerToString(shown) + " signal(s) in 24 h", clrSilver);
      ShowLine(3, "Updated " + JsonString(head, "updated_utc") + " | never trades", clrGray);
     }
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
void DrawSignal(const string line)
  {
   string id      = JsonString(line, "id");
   string dir     = JsonString(line, "dir");
   string status  = JsonString(line, "status");
   string swept   = JsonString(line, "pool_label");
   datetime start = (datetime)(long)JsonNumber(line, "t");
   double score   = JsonNumber(line, "score");
   double entry   = JsonNumber(line, "entry");
   double sl      = JsonNumber(line, "sl");
   double tp1     = JsonNumber(line, "tp1");
   double tp2     = JsonNumber(line, "tp2");
   double tp3     = JsonNumber(line, "tp3");
   double pool    = JsonNumber(line, "pool");
   if(id == "" || start <= 0 || entry <= 0 || sl <= 0 || tp1 <= 0)
      return;
   bool buy = (dir == "BUY");
   color main = buy ? InpBuyColor : InpSellColor;
   int bar = PeriodSeconds(_Period);
   datetime finish = start + (datetime)(bar * InpBoxBars);
   double farthest = tp1;
   if(tp2 > 0)
      farthest = tp2;
   if(tp3 > 0)
      farthest = tp3;
   string base = PREFIX + id + "_";

   Box(base + "risk", start, entry, finish, sl, InpRiskFill);
   Box(base + "reward", start, entry, finish, farthest, InpRewardFill);
   Level(base + "entry", start, finish, entry, main, STYLE_SOLID, 2, "Entry " + Price(entry));
   Level(base + "sl", start, finish, sl, InpStopColor, STYLE_SOLID, 1, "SL " + Price(sl));
   Level(base + "tp1", start, finish, tp1, InpTargetColor, STYLE_DASH, 1, "TP1 " + Price(tp1));
   if(tp2 > 0)
      Level(base + "tp2", start, finish, tp2, InpTargetColor, STYLE_DASH, 1, "TP2 " + Price(tp2));
   if(tp3 > 0)
      Level(base + "tp3", start, finish, tp3, InpTargetColor, STYLE_DASH, 1, "TP3 " + Price(tp3));
   if(pool > 0)
      Level(base + "pool", start - (datetime)(bar * 6), start, pool, InpPoolColor, STYLE_DOT, 1,
            "Swept " + swept + " " + Price(pool));

   string text = (buy ? "BUY " : "SELL ") + DoubleToString(score, 0) + " | " + status;
   Text(base + "label", start, sl, text, main, buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER);
  }

//+------------------------------------------------------------------+
//| The scalper (v2)                                                 |
//+------------------------------------------------------------------+
void DrawScalper(string &lines[], const int count)
  {
   ObjectsDeleteAll(0, SPREFIX);
   if(count == 0)
     {
      ChartRedraw(0);
      return;
     }
   string head = lines[0];
   g_state = JsonString(head, "state");
   g_updated_gmt = (long)JsonNumber(head, "updated_gmt");
   double previous_value = 0.0;
   datetime previous_time = 0;
   int previous_up = 0;
   int steps = 0;
   for(int i = 1; i < count; i++)
     {
      string line = lines[i];
      string kind = JsonString(line, "k");
      if(kind == "step" && InpShowStepLine)
        {
         datetime t = (datetime)(long)JsonNumber(line, "t");
         double value = JsonNumber(line, "v");
         int up = (int)JsonNumber(line, "up");
         if(previous_time > 0 && t > previous_time)
           {
            color clr = (previous_up == 1) ? InpStepUp : InpStepDown;
            Segment(SPREFIX + "step_h" + IntegerToString(steps), previous_time, previous_value, t,
                    previous_value, clr);
            if(value != previous_value)
               Segment(SPREFIX + "step_v" + IntegerToString(steps), t, previous_value, t, value,
                       (up == 1) ? InpStepUp : InpStepDown);
            steps++;
           }
         previous_time = t;
         previous_value = value;
         previous_up = up;
        }
      else
         if(kind == "x" && InpShowSweeps)
           {
            datetime t = (datetime)(long)JsonNumber(line, "t");
            double p = JsonNumber(line, "p");
            bool high = (JsonString(line, "s") == "high");
            Text(SPREFIX + "x_" + IntegerToString(i), t, p, "x", InpGold,
                 high ? ANCHOR_LOWER : ANCHOR_UPPER);
           }
         else
            if(kind == "scalp" && InpShowScalper)
               DrawScalp(line);
            else
               if(kind == "hit" && InpShowScalper)
                 {
                  datetime t = (datetime)(long)JsonNumber(line, "t");
                  double p = JsonNumber(line, "p");
                  Text(SPREFIX + "hit_" + IntegerToString(i), t, p, JsonString(line, "tp"),
                       InpTargetColor, ANCHOR_LOWER);
                 }
     }
   if(InpShowDashboard)
      Dashboard(head);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
void DrawScalp(const string line)
  {
   string id      = JsonString(line, "id");
   string dir     = JsonString(line, "dir");
   string mode    = JsonString(line, "mode");
   string where   = JsonString(line, "where");
   string status  = JsonString(line, "status");
   string reason  = JsonString(line, "exit");
   datetime start = (datetime)(long)JsonNumber(line, "t");
   datetime end   = (datetime)(long)JsonNumber(line, "end");
   double entry   = JsonNumber(line, "entry");
   double sl      = JsonNumber(line, "sl");
   double stop    = JsonNumber(line, "stop");
   double tp1     = JsonNumber(line, "tp1");
   double tp2     = JsonNumber(line, "tp2");
   double tp3     = JsonNumber(line, "tp3");
   double r       = JsonNumber(line, "r");
   if(id == "" || start <= 0 || entry <= 0 || sl <= 0 || tp1 <= 0)
      return;
   bool buy = (dir == "BUY");
   bool open = (status == "open");
   color main = buy ? InpBuyColor : InpSellColor;
   int bar = PeriodSeconds(_Period);
   datetime finish = (open || end <= start) ? start + (datetime)(bar * InpScalpBoxBars) : end;
   double farthest = tp1;
   if(tp2 > 0)
      farthest = tp2;
   if(tp3 > 0)
      farthest = tp3;
   string base = SPREFIX + id + "_";

   Box(base + "risk", start, entry, finish, sl, InpRiskFill);
   Box(base + "reward", start, entry, finish, farthest, InpRewardFill);
   Level(base + "entry", start, finish, entry, InpGold, STYLE_SOLID, 1, "ENTRY " + Price(entry));
   Level(base + "sl", start, finish, sl, InpStopColor, STYLE_SOLID, 1, "SL " + Price(sl));
   Level(base + "tp1", start, finish, tp1, InpTargetColor, STYLE_DASH, 1, "TP1 " + Price(tp1));
   if(tp2 > 0)
      Level(base + "tp2", start, finish, tp2, InpTargetColor, STYLE_DASH, 1, "TP2 " + Price(tp2));
   if(tp3 > 0)
      Level(base + "tp3", start, finish, tp3, InpTargetColor, STYLE_DASH, 1, "TP3 " + Price(tp3));
   if(open && stop > 0 && MathAbs(stop - sl) > _Point)
      Level(base + "stop", start, finish, stop, InpStopColor, STYLE_DOT, 1, "stop " + Price(stop));

   string tag = (buy ? "BUY " : "SELL ") + mode + " " + where;
   Text(base + "tag", start, sl, tag, main, buy ? ANCHOR_LEFT_UPPER : ANCHOR_LEFT_LOWER);
   string result = open ? StringFormat("open %+.2f R", r) : StringFormat("%+.2f R (%s)", r, reason);
   Text(base + "result", finish, entry, result, (r >= 0) ? InpBuyColor : InpSellColor,
        ANCHOR_RIGHT_LOWER);
  }

//+------------------------------------------------------------------+
//| The scalper's dashboard box (bottom-left)                        |
//+------------------------------------------------------------------+
void Dashboard(const string head)
  {
   int x = 10;
   int y = InpDashboardY;
   int width = 600;
   int height = 150;
   string back = SPREFIX + "dash_back";
   ObjectCreate(0, back, OBJ_RECTANGLE_LABEL, 0, 0, 0);
   ObjectSetInteger(0, back, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, back, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, back, OBJPROP_YDISTANCE, y + height);
   ObjectSetInteger(0, back, OBJPROP_XSIZE, width);
   ObjectSetInteger(0, back, OBJPROP_YSIZE, height);
   ObjectSetInteger(0, back, OBJPROP_BGCOLOR, InpPanelBack);
   ObjectSetInteger(0, back, OBJPROP_BORDER_TYPE, BORDER_FLAT);
   ObjectSetInteger(0, back, OBJPROP_COLOR, InpGold);
   ObjectSetInteger(0, back, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, back, OBJPROP_HIDDEN, true);

   string where = JsonString(head, "where");
   string status = JsonString(head, "status");
   string mode = JsonString(head, "mode");
   string diamond = ShortToString(0x25C6);
   Label("dash_title", x + 8, y + height - 6, diamond + " GoldSweep Scalper", InpGold, 10);
   Label("dash_badge", x + 190, y + height - 6, where, (where == "DEMO") ? InpSellColor : clrSilver, 10);
   Label("dash_status", x + 8, y + height - 26,
         status + " | mode=" + mode + " | strategies=" + JsonString(head, "strategies") +
         " | trading " + JsonString(head, "state"), StatusColor(status), 9);

   // Column 1: the market.
   int row = y + height - 48;
   Label("m1", x + 8, row, "Trend (" + JsonString(head, "tf") + "): " + JsonString(head, "trend"), clrSilver, 8);
   Label("m2", x + 8, row - 16, "ATR: " + DoubleToString(JsonNumber(head, "atr"), 2), clrSilver, 8);
   Label("m3", x + 8, row - 32, "Session: " + JsonString(head, "session"), clrSilver, 8);
   Label("m4", x + 8, row - 48, "Spread: " + DoubleToString(JsonNumber(head, "spread"), 0) +
         " pts (median " + DoubleToString(JsonNumber(head, "spread_median"), 0) + ")", clrSilver, 8);
   string news = JsonString(head, "news");
   Label("m5", x + 8, row - 64, "News: " + (news == "" ? "none ahead" : news), clrSilver, 8);

   // Column 2: the active scalp.
   string active = JsonString(head, "active");
   if(active == "")
      Label("t1", x + 210, row, "No scalp open", clrSilver, 8);
   else
     {
      int hit = (int)JsonNumber(head, "active_hit");
      Label("t1", x + 210, row, JsonString(head, "active_dir") + " " + active, clrWhite, 8);
      Label("t2", x + 210, row - 16, "Entry " + Price(JsonNumber(head, "active_entry")) + "  stop " +
            Price(JsonNumber(head, "active_stop")), clrSilver, 8);
      Label("t3", x + 210, row - 32, "TP1 " + Price(JsonNumber(head, "active_tp1")) + (hit >= 1 ? " ok" : ""),
            InpTargetColor, 8);
      Label("t4", x + 210, row - 48, "TP2 " + Price(JsonNumber(head, "active_tp2")) + (hit >= 2 ? " ok" : "") +
            "  TP3 " + Price(JsonNumber(head, "active_tp3")) + (hit >= 3 ? " ok" : ""), InpTargetColor, 8);
      Label("t5", x + 210, row - 64, StringFormat("%+.2f R | parts %s | %d min",
            JsonNumber(head, "active_r"), JsonString(head, "active_children"),
            (int)JsonNumber(head, "active_min")), clrSilver, 8);
     }

   // Column 3: results after costs.
   Label("p1", x + 410, row, "Today: " + Results(head, "today"), clrSilver, 8);
   Label("p2", x + 410, row - 16, StringFormat("  won %.0f%% (BE %.0f%%)",
         100 * JsonNumber(head, "today_win"), 100 * JsonNumber(head, "today_be")), clrSilver, 8);
   Label("p3", x + 410, row - 32, "30 days: " + Results(head, "d30"), clrSilver, 8);
   Label("p4", x + 410, row - 48, StringFormat("  won %.0f%% (BE %.0f%%) PF %.2f",
         100 * JsonNumber(head, "d30_win"), 100 * JsonNumber(head, "d30_be"),
         JsonNumber(head, "d30_pf")), clrSilver, 8);
   Label("p5", x + 410, row - 64, "Updated " + JsonString(head, "updated_utc"), clrGray, 8);

   Button(BUTTON_PAUSE, x + width - 180, y + height - 4, (g_state == "active") ? "Pause" : "Resume");
   Button(BUTTON_OFF, x + width - 92, y + height - 4, "Scalper off");
   ShowStale();
  }

//+------------------------------------------------------------------+
string Results(const string head, const string prefix)
  {
   return(StringFormat("%d scalps %+.2f R %+.2f", (int)JsonNumber(head, prefix + "_n"),
                       JsonNumber(head, prefix + "_r"), JsonNumber(head, prefix + "_net")));
  }

//+------------------------------------------------------------------+
color StatusColor(const string status)
  {
   if(status == "READY")
      return(InpBuyColor);
   if(status == "IN TRADE")
      return(InpGold);
   if(status == "PAUSED")
      return(clrOrange);
   return(clrSilver);
  }

//+------------------------------------------------------------------+
//| STALE: the bot has not rewritten scalp.json for a while          |
//+------------------------------------------------------------------+
void ShowStale()
  {
   string name = SPREFIX + "dash_stale";
   if(!InpShowDashboard || g_updated_gmt <= 0)
      return;
   long age = (long)TimeGMT() - g_updated_gmt;
   if(age <= InpStaleSec)
     {
      ObjectDelete(0, name);
      return;
     }
   Label("dash_stale", 300, InpDashboardY + 150 - 6,
         StringFormat("STALE: no update for %d min (is the bot running?)", (int)(age / 60)),
         InpSellColor, 9);
   ChartRedraw(0);
  }

//+------------------------------------------------------------------+
void Label(const string id, const int x, const int y, const string text, const color clr,
           const int size)
  {
   string name = SPREFIX + id;
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_UPPER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, size);
   ObjectSetString(0, name, OBJPROP_FONT, "Segoe UI");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void Button(const string name, const int x, const int y, const string text)
  {
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_BUTTON, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, x);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, y);
   ObjectSetInteger(0, name, OBJPROP_XSIZE, 84);
   ObjectSetInteger(0, name, OBJPROP_YSIZE, 20);
   ObjectSetInteger(0, name, OBJPROP_BGCOLOR, C'40,44,52');
   ObjectSetInteger(0, name, OBJPROP_COLOR, InpGold);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_STATE, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
string Price(const double value)
  {
   return(DoubleToString(value, _Digits));
  }

//+------------------------------------------------------------------+
void Box(const string name, const datetime t1, const double p1,
         const datetime t2, const double p2, const color fill)
  {
   if(!ObjectCreate(0, name, OBJ_RECTANGLE, 0, t1, p1, t2, p2))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, fill);
   ObjectSetInteger(0, name, OBJPROP_FILL, true);
   ObjectSetInteger(0, name, OBJPROP_BACK, true);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void Segment(const string name, const datetime t1, const double p1, const datetime t2,
             const double p2, const color clr)
  {
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t1, p1, t2, p2))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, 2);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void Level(const string name, const datetime t1, const datetime t2, const double price,
           const color clr, const ENUM_LINE_STYLE style, const int width, const string text)
  {
   if(!ObjectCreate(0, name, OBJ_TREND, 0, t1, price, t2, price))
      return;
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_STYLE, style);
   ObjectSetInteger(0, name, OBJPROP_WIDTH, width);
   ObjectSetInteger(0, name, OBJPROP_RAY_RIGHT, false);
   ObjectSetInteger(0, name, OBJPROP_RAY_LEFT, false);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
   ObjectSetString(0, name, OBJPROP_TOOLTIP, text);
   Text(name + "_text", t2, price, text, clr, ANCHOR_LEFT);
  }

//+------------------------------------------------------------------+
void Text(const string name, const datetime t, const double price, const string text,
          const color clr, const ENUM_ANCHOR_POINT anchor)
  {
   if(!ObjectCreate(0, name, OBJ_TEXT, 0, t, price))
      return;
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetString(0, name, OBJPROP_FONT, "Segoe UI");
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 8);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, anchor);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
//| One line of the main strategy's corner box (bottom-left)         |
//+------------------------------------------------------------------+
void ShowLine(const int row, const string text, const color clr)
  {
   string name = PREFIX + "panel_" + IntegerToString(row);
   if(ObjectFind(0, name) < 0)
      ObjectCreate(0, name, OBJ_LABEL, 0, 0, 0);
   ObjectSetInteger(0, name, OBJPROP_CORNER, CORNER_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
   ObjectSetInteger(0, name, OBJPROP_XDISTANCE, 10);
   ObjectSetInteger(0, name, OBJPROP_YDISTANCE, 16 + (3 - row) * 16);
   ObjectSetInteger(0, name, OBJPROP_COLOR, clr);
   ObjectSetInteger(0, name, OBJPROP_FONTSIZE, 9);
   ObjectSetString(0, name, OBJPROP_FONT, "Consolas");
   ObjectSetString(0, name, OBJPROP_TEXT, text);
   ObjectSetInteger(0, name, OBJPROP_SELECTABLE, false);
   ObjectSetInteger(0, name, OBJPROP_HIDDEN, true);
  }

//+------------------------------------------------------------------+
void ShowStatus(const string text, const color clr)
  {
   ObjectsDeleteAll(0, PREFIX);
   ShowLine(3, text, clr);
   ChartRedraw(0);
  }
//+------------------------------------------------------------------+
