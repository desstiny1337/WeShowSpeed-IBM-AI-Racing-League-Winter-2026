#!/usr/bin/python
# snakeoil.py
# Chris X Edwards <snakeoil@xed.ch>
# Snake Oil is a Python library for interfacing with a TORCS
# race car simulator which has been patched with the server
# extentions used in the Simulated Car Racing competitions.
# http://scr.geccocompetitions.com/
#
# To use it, you must import it and create a "drive()" function.
# This will take care of option handling and server connecting, etc.
# To see how to write your own client do something like this which is
# a complete working client:
# /-----------------------------------------------\
# |#!/usr/bin/python                              |
# |import snakeoil                                |
# |if __name__ == "__main__":                     |
# |    C= snakeoil.Client()                       |
# |    for step in xrange(C.maxSteps,0,-1):       |
# |        C.get_servers_input()                  |
# |        snakeoil.drive_example(C)              |
# |        C.respond_to_server()                  |
# |    C.shutdown()                               |
# \-----------------------------------------------/
# This should then be a full featured client. The next step is to
# replace 'snakeoil.drive_example()' with your own. There is a
# dictionary which holds various option values (see `default_options`
# variable for all the details) but you probably only need a few
# things from it. Mainly the `trackname` and `stage` are important
# when developing a strategic bot.
#
# This dictionary also contains a ServerState object
# (key=S) and a DriverAction object (key=R for response). This allows
# you to get at all the information sent by the server and to easily
# formulate your reply. These objects contain a member dictionary "d"
# (for data dictionary) which contain key value pairs based on the
# server's syntax. Therefore, you can read the following:
#    angle, curLapTime, damage, distFromStart, distRaced, focus,
#    fuel, gear, lastLapTime, opponents, racePos, rpm,
#    speedX, speedY, speedZ, track, trackPos, wheelSpinVel, z
# The syntax specifically would be something like:
#    X= o[S.d['tracPos']]
# And you can set the following:
#    accel, brake, clutch, gear, steer, focus, meta
# The syntax is:
#     o[R.d['steer']]= X
# Note that it is 'steer' and not 'steering' as described in the manual!
# All values should be sensible for their type, including lists being lists.
# See the SCR manual or http://xed.ch/help/torcs.html for details.
#
# If you just run the snakeoil.py base library itself it will implement a
# serviceable client with a demonstration drive function that is
# sufficient for getting around most tracks.
# Try `snakeoil.py --help` to get started.

# for Python3-based torcs python robot client
import socket
import sys
import getopt
import os
import time
import csv
import json
import math
PI= 3.14159265359

data_size = 2**17

# == Telemetry logger settings (used only with --log) ==
LOG_FLUSH_EVERY= 50       # Flush CSV to disk every N rows (50 rows ~ 1 s of driving).
OFFTRACK_TRACKPOS= 1.0    # |trackPos| above this counts as off track.
LOG_TRACK_AHEAD_IDX= 9    # Index of the straight-ahead range finder in 'track' (0 deg).

# == Driver parameters (spec 0008) ==
# Every tunable number of drive_example() lives here. Override any subset with
# --params <file.json> (see params/default.json). Units: speeds km/h, distances m.
DEFAULT_PARAMS= {
    # -- Target speed from the track range finders --
    'v_max': 300.0,             # km/h, upper clamp of the target speed.
    'v_min': 50.0,              # km/h, lower clamp of the target speed while on track.
    'v_corner': 80.0,           # km/h, speed we want to have when reaching the obstacle seen ahead.
    'decel': 12.0,              # m/s^2, assumed braking deceleration in v^2 = v_corner^2 + 2*decel*d.
    'dist_margin': 15.0,        # m, safety margin subtracted from the free distance ahead.
    'ahead_halfwidth': 2,       # int, distance ahead = max(track[9-k .. 9+k]) (k=2 -> +-1 deg).
    'curv_speed_gain': 0.0,     # km/h subtracted per degree of the longest-ray direction (0 = off).
    # -- Throttle / brake --
    'accel_gain': 0.1,          # throttle per km/h below (target + brake_hyst).
    'brake_gain': 0.1,          # brake per km/h above target (10 km/h over -> full brake).
    'brake_hyst': 2.0,          # km/h, dead band above target before braking starts.
    'wheel_radius_front': 0.3024,  # m, car1-ow1 front wheel (12" rim + 150 mm tyre).
    'wheel_radius_rear': 0.3151,   # m, car1-ow1 rear wheel (13" rim + 150 mm tyre).
    'abs_slip': 0.15,           # ABS: wheel slip (v_car - v_wheel)/v_car above which brake is reduced.
    'abs_gain': 3.0,            # ABS: brake *= 1 - abs_gain*(slip - abs_slip), clipped to 0..1.
    'abs_min_speed': 10.0,      # km/h, ABS is off below this speed.
    'tcs_slip': 0.15,           # TCS: rear slip (v_wheel - v_car)/v_car above which throttle is reduced.
    'tcs_gain': 3.0,            # TCS: accel *= 1 - tcs_gain*(slip - tcs_slip), clipped to 0..1.
    'tcs_min_speed': 10.0,      # km/h, TCS is off below this speed (lets the car launch).
    # -- Steering (steer > 0 = left) --
    'steer_angle_gain': 4.775,  # steer per rad of 'angle' (old value 15/PI).
    'steer_pos_gain': 0.10,     # steer per unit of trackPos (pulls to the centre).
    'steer_ray_gain': 0.0,      # steer per rad toward the longest range finder (0 = off).
    # -- Gears (by rpm; car1-ow1 rev limiter 18700) --
    'gear_up_rpm': 17500.0,     # shift up above this rpm.
    'gear_down_rpm': 11000.0,   # shift down below this rpm.
    'gear_max': 6,              # int, highest gear used (1..6).
    # -- Off-track recovery (|trackPos| > 1) --
    'offtrack_speed': 50.0,     # km/h, target speed cap while off track.
    'offtrack_pos_gain': 0.5,   # steer per unit of trackPos while off track (replaces steer_pos_gain).
    # -- Stuck recovery (reverse) --
    'stuck_speed': 5.0,         # km/h, |speedX| below this counts as not moving.
    'stuck_angle': 0.5236,      # rad (30 deg), |angle| above this counts as badly turned.
    'stuck_steps': 100,         # int, steps (~2 s) not moving + turned before reversing.
    'reverse_steps': 100,       # int, steps to drive in reverse gear.
    'reverse_accel': 0.5,       # throttle while reversing.
    'reverse_steer_gain': 1.0,  # steer = -gain*angle while reversing (inverted).
}
# Angles (deg) of the 19 'track' range finders; must match the init string in
# Client.setup_connection(). Negative = left of the car axis, positive = right.
TRACK_SENSOR_ANGLES= [-45, -19, -12, -7, -4, -2.5, -1.7, -1, -.5, 0,
                      .5, 1, 1.7, 2.5, 4, 7, 12, 19, 45]

def load_params(path):
    '''Load a JSON object from `path` and return DEFAULT_PARAMS overridden by
    it. Raises ValueError on unreadable file, unknown keys or non-numbers.'''
    try:
        with open(path, encoding='utf-8') as f:
            data= json.load(f)
    except (OSError, ValueError) as why:
        raise ValueError('cannot read params file %s: %s' % (path, why))
    if not isinstance(data, dict):
        raise ValueError('params file %s must contain a JSON object' % path)
    unknown= sorted(set(data) - set(DEFAULT_PARAMS))
    if unknown:
        raise ValueError('unknown parameter(s) in %s: %s\nknown: %s' % (
            path, ', '.join(unknown), ', '.join(sorted(DEFAULT_PARAMS))))
    P= dict(DEFAULT_PARAMS)
    for k, v in data.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError('parameter %s in %s must be a number, got %r' % (k, path, v))
        P[k]= v
    return P

# Initialize help messages
ophelp=  'Options:\n'
ophelp+= ' --host, -H <host>    TORCS server host. [localhost]\n'
ophelp+= ' --port, -p <port>    TORCS port. [3001]\n'
ophelp+= ' --id, -i <id>        ID for server. [SCR]\n'
ophelp+= ' --steps, -m <#>      Maximum simulation steps. 1 sec ~ 50 steps. [100000]\n'
ophelp+= ' --episodes, -e <#>   Maximum learning episodes. [1]\n'
ophelp+= ' --track, -t <track>  Your name for this track. Used for learning. [unknown]\n'
ophelp+= ' --stage, -s <#>      0=warm up, 1=qualifying, 2=race, 3=unknown. [3]\n'
ophelp+= ' --debug, -d          Output full telemetry.\n'
ophelp+= ' --log, -l <file.csv> Write per-step telemetry CSV and print a run summary.\n'
ophelp+= ' --params, -P <file.json> Override driver parameters (see params/default.json).\n'
ophelp+= ' --help, -h           Show this help.\n'
ophelp+= ' --version, -v        Show current version.'
usage= 'Usage: %s [ophelp [optargs]] \n' % sys.argv[0]
usage= usage + ophelp
version= "20130505-2"

def clip(v,lo,hi):
    if v<lo: return lo
    elif v>hi: return hi
    else: return v

def bargraph(x,mn,mx,w,c='X'):
    '''Draws a simple asciiart bar graph. Very handy for
    visualizing what's going on with the data.
    x= Value from sensor, mn= minimum plottable value,
    mx= maximum plottable value, w= width of plot in chars,
    c= the character to plot with.'''
    if not w: return '' # No width!
    if x<mn: x= mn      # Clip to bounds.
    if x>mx: x= mx      # Clip to bounds.
    tx= mx-mn # Total real units possible to show on graph.
    if tx<=0: return 'backwards' # Stupid bounds.
    upw= tx/float(w) # X Units per output char width.
    if upw<=0: return 'what?' # Don't let this happen.
    negpu, pospu, negnonpu, posnonpu= 0,0,0,0
    if mn < 0: # Then there is a negative part to graph.
        if x < 0: # And the plot is on the negative side.
            negpu= -x + min(0,mx)
            negnonpu= -mn + x
        else: # Plot is on pos. Neg side is empty.
            negnonpu= -mn + min(0,mx) # But still show some empty neg.
    if mx > 0: # There is a positive part to the graph
        if x > 0: # And the plot is on the positive side.
            pospu= x - max(0,mn)
            posnonpu= mx - x
        else: # Plot is on neg. Pos side is empty.
            posnonpu= mx - max(0,mn) # But still show some empty pos.
    nnc= int(negnonpu/upw)*'-'
    npc= int(negpu/upw)*c
    ppc= int(pospu/upw)*c
    pnc= int(posnonpu/upw)*'_'
    return '[%s]' % (nnc+npc+ppc+pnc)

class Client():
    def __init__(self,H=None,p=None,i=None,e=None,t=None,s=None,d=None,vision=False):
        # If you don't like the option defaults,  change them here.
        self.vision = vision

        self.host= 'localhost'
        self.port= 3001
        self.sid= 'SCR'
        self.maxEpisodes=1 # "Maximum number of learning episodes to perform"
        self.trackname= 'unknown'
        self.stage= 3 # 0=Warm-up, 1=Qualifying 2=Race, 3=unknown <Default=3>
        self.debug= False
        self.maxSteps= 100000  # 50steps/second
        self.logFile= None # Telemetry CSV path (--log); None = no logging.
        self.paramsFile= None # Driver params JSON (--params); None = DEFAULT_PARAMS.
        self.params= dict(DEFAULT_PARAMS) # Effective driver params used by drive_example().
        self.parse_the_command_line()
        if H: self.host= H
        if p: self.port= p
        if i: self.sid= i
        if e: self.maxEpisodes= e
        if t: self.trackname= t
        if s: self.stage= s
        if d: self.debug= d
        self.S= ServerState()
        self.R= DriverAction()
        self.setup_connection()

    def setup_connection(self):
        # == Set Up UDP Socket ==
        try:
            self.so= socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except socket.error as emsg:
            print('Error: Could not create socket...')
            sys.exit(-1)
        # == Initialize Connection To Server ==
        self.so.settimeout(1)

        n_fail = 5
        while True:
            # This string establishes track sensor angles! You can customize them.
            #a= "-90 -75 -60 -45 -30 -20 -15 -10 -5 0 5 10 15 20 30 45 60 75 90"
            # xed- Going to try something a bit more aggressive...
            a= "-45 -19 -12 -7 -4 -2.5 -1.7 -1 -.5 0 .5 1 1.7 2.5 4 7 12 19 45"

            initmsg='%s(init %s)' % (self.sid,a)

            try:
                self.so.sendto(initmsg.encode(), (self.host, self.port))
            except socket.error as emsg:
                sys.exit(-1)
            sockdata= str()
            try:
                sockdata,addr= self.so.recvfrom(data_size)
                sockdata = sockdata.decode('utf-8')
            except socket.error as emsg:
                print("Waiting for server on %d............" % self.port)
                print("Count Down : " + str(n_fail))
                if n_fail < 0:
                    print("relaunch torcs")
                    os.system('pkill torcs')
                    time.sleep(1.0)
                    if self.vision is False:
                        os.system('torcs -nofuel -nodamage -nolaptime &')
                    else:
                        os.system('torcs -nofuel -nodamage -nolaptime -vision &')

                    time.sleep(1.0)
                    os.system('sh autostart.sh')
                    n_fail = 5
                n_fail -= 1

            identify = '***identified***'
            if identify in sockdata:
                print("Client connected on %d.............." % self.port)
                break

    def parse_the_command_line(self):
        try:
            (opts, args) = getopt.getopt(sys.argv[1:], 'H:p:i:m:e:t:s:l:P:dhv',
                       ['host=','port=','id=','steps=',
                        'episodes=','track=','stage=','log=','params=',
                        'debug','help','version'])
        except getopt.error as why:
            print('getopt error: %s\n%s' % (why, usage))
            sys.exit(-1)
        try:
            for opt in opts:
                if opt[0] == '-h' or opt[0] == '--help':
                    print(usage)
                    sys.exit(0)
                if opt[0] == '-d' or opt[0] == '--debug':
                    self.debug= True
                if opt[0] == '-H' or opt[0] == '--host':
                    self.host= opt[1]
                if opt[0] == '-i' or opt[0] == '--id':
                    self.sid= opt[1]
                if opt[0] == '-t' or opt[0] == '--track':
                    self.trackname= opt[1]
                if opt[0] == '-s' or opt[0] == '--stage':
                    self.stage= int(opt[1])
                if opt[0] == '-p' or opt[0] == '--port':
                    self.port= int(opt[1])
                if opt[0] == '-e' or opt[0] == '--episodes':
                    self.maxEpisodes= int(opt[1])
                if opt[0] == '-m' or opt[0] == '--steps':
                    self.maxSteps= int(opt[1])
                if opt[0] == '-l' or opt[0] == '--log':
                    self.logFile= opt[1]
                if opt[0] == '-P' or opt[0] == '--params':
                    self.paramsFile= opt[1]
                    try:
                        self.params= load_params(opt[1])
                    except ValueError as why:
                        print('Error: %s' % why)
                        sys.exit(-1)
                if opt[0] == '-v' or opt[0] == '--version':
                    print('%s %s' % (sys.argv[0], version))
                    sys.exit(0)
        except ValueError as why:
            print('Bad parameter \'%s\' for option %s: %s\n%s' % (
                                       opt[1], opt[0], why, usage))
            sys.exit(-1)
        if len(args) > 0:
            print('Superflous input? %s\n%s' % (', '.join(args), usage))
            sys.exit(-1)

    def get_servers_input(self):
        '''Server's input is stored in a ServerState object'''
        if not self.so: return
        sockdata= str()

        while True:
            try:
                # Receive server data
                sockdata,addr= self.so.recvfrom(data_size)
                sockdata = sockdata.decode('utf-8')
            except socket.error as emsg:
                print('.', end=' ')
                #print "Waiting for data on %d.............." % self.port
            if '***identified***' in sockdata:
                print("Client connected on %d.............." % self.port)
                continue
            elif '***shutdown***' in sockdata:
                print((("Server has stopped the race on %d. "+
                        "You were in %d place.") %
                        (self.port,self.S.d['racePos'])))
                self.shutdown()
                return
            elif '***restart***' in sockdata:
                # What do I do here?
                print("Server has restarted the race on %d." % self.port)
                # I haven't actually caught the server doing this.
                self.shutdown()
                return
            elif not sockdata: # Empty?
                continue       # Try again.
            else:
                self.S.parse_server_str(sockdata)
                if self.debug:
                    sys.stderr.write("\x1b[2J\x1b[H") # Clear for steady output.
                    print(self.S)
                break # Can now return from this function.

    def respond_to_server(self):
        if not self.so: return
        try:
            message = repr(self.R)
            self.so.sendto(message.encode(), (self.host, self.port))
        except socket.error as emsg:
            print("Error sending to server: %s Message %s" % (emsg[1],str(emsg[0])))
            sys.exit(-1)
        if self.debug: print(self.R.fancyout())
        # Or use this for plain output:
        #if self.debug: print self.R

    def shutdown(self):
        if not self.so: return
        print(("Race terminated or %d steps elapsed. Shutting down %d."
               % (self.maxSteps,self.port)))
        self.so.close()
        self.so = None
        #sys.exit() # No need for this really.

class ServerState():
    '''What the server is reporting right now.'''
    def __init__(self):
        self.servstr= str()
        self.d= dict()

    def parse_server_str(self, server_string):
        '''Parse the server string.'''
        self.servstr= server_string.strip()[:-1]
        sslisted= self.servstr.strip().lstrip('(').rstrip(')').split(')(')
        for i in sslisted:
            w= i.split(' ')
            self.d[w[0]]= destringify(w[1:])

    def __repr__(self):
        # Comment the next line for raw output:
        return self.fancyout()
        # -------------------------------------
        out= str()
        for k in sorted(self.d):
            strout= str(self.d[k])
            if type(self.d[k]) is list:
                strlist= [str(i) for i in self.d[k]]
                strout= ', '.join(strlist)
            out+= "%s: %s\n" % (k,strout)
        return out

    def fancyout(self):
        '''Specialty output for useful ServerState monitoring.'''
        out= str()
        sensors= [ # Select the ones you want in the order you want them.
        #'curLapTime',
        #'lastLapTime',
        'stucktimer',
        #'damage',
        #'focus',
        'fuel',
        #'gear',
        'distRaced',
        'distFromStart',
        #'racePos',
        'opponents',
        'wheelSpinVel',
        'z',
        'speedZ',
        'speedY',
        'speedX',
        'targetSpeed',
        'rpm',
        'skid',
        'slip',
        'track',
        'trackPos',
        'angle',
        ]

        #for k in sorted(self.d): # Use this to get all sensors.
        for k in sensors:
            if type(self.d.get(k)) is list: # Handle list type data.
                if k == 'track': # Nice display for track sensors.
                    strout= str()
                 #  for tsensor in self.d['track']:
                 #      if   tsensor >180: oc= '|'
                 #      elif tsensor > 80: oc= ';'
                 #      elif tsensor > 60: oc= ','
                 #      elif tsensor > 39: oc= '.'
                 #      #elif tsensor > 13: oc= chr(int(tsensor)+65-13)
                 #      elif tsensor > 13: oc= chr(int(tsensor)+97-13)
                 #      elif tsensor >  3: oc= chr(int(tsensor)+48-3)
                 #      else: oc= '_'
                 #      strout+= oc
                 #  strout= ' -> '+strout[:9] +' ' + strout[9] + ' ' + strout[10:]+' <-'
                    raw_tsens= ['%.1f'%x for x in self.d['track']]
                    strout+= ' '.join(raw_tsens[:9])+'_'+raw_tsens[9]+'_'+' '.join(raw_tsens[10:])
                elif k == 'opponents': # Nice display for opponent sensors.
                    strout= str()
                    for osensor in self.d['opponents']:
                        if   osensor >190: oc= '_'
                        elif osensor > 90: oc= '.'
                        elif osensor > 39: oc= chr(int(osensor/2)+97-19)
                        elif osensor > 13: oc= chr(int(osensor)+65-13)
                        elif osensor >  3: oc= chr(int(osensor)+48-3)
                        else: oc= '?'
                        strout+= oc
                    strout= ' -> '+strout[:18] + ' ' + strout[18:]+' <-'
                else:
                    strlist= [str(i) for i in self.d[k]]
                    strout= ', '.join(strlist)
            else: # Not a list type of value.
                if k == 'gear': # This is redundant now since it's part of RPM.
                    gs= '_._._._._._._._._'
                    p= int(self.d['gear']) * 2 + 2  # Position
                    l= '%d'%self.d['gear'] # Label
                    if l=='-1': l= 'R'
                    if l=='0':  l= 'N'
                    strout= gs[:p]+ '(%s)'%l + gs[p+3:]
                elif k == 'damage':
                    strout= '%6.0f %s' % (self.d[k], bargraph(self.d[k],0,10000,50,'~'))
                elif k == 'fuel':
                    strout= '%6.0f %s' % (self.d[k], bargraph(self.d[k],0,100,50,'f'))
                elif k == 'speedX':
                    cx= 'X'
                    if self.d[k]<0: cx= 'R'
                    strout= '%6.1f %s' % (self.d[k], bargraph(self.d[k],-30,300,50,cx))
                elif k == 'speedY': # This gets reversed for display to make sense.
                    strout= '%6.1f %s' % (self.d[k], bargraph(self.d[k]*-1,-25,25,50,'Y'))
                elif k == 'speedZ':
                    strout= '%6.1f %s' % (self.d[k], bargraph(self.d[k],-13,13,50,'Z'))
                elif k == 'z':
                    strout= '%6.3f %s' % (self.d[k], bargraph(self.d[k],.3,.5,50,'z'))
                elif k == 'trackPos': # This gets reversed for display to make sense.
                    cx='<'
                    if self.d[k]<0: cx= '>'
                    strout= '%6.3f %s' % (self.d[k], bargraph(self.d[k]*-1,-1,1,50,cx))
                elif k == 'stucktimer':
                    if self.d[k]:
                        strout= '%3d %s' % (self.d[k], bargraph(self.d[k],0,300,50,"'"))
                    else: strout= 'Not stuck!'
                elif k == 'rpm':
                    g= self.d['gear']
                    if g < 0:
                        g= 'R'
                    else:
                        g= '%1d'% g
                    strout= bargraph(self.d[k],0,10000,50,g)
                elif k == 'angle':
                    asyms= [
                          "  !  ", ".|'  ", "./'  ", "_.-  ", ".--  ", "..-  ",
                          "---  ", ".__  ", "-._  ", "'-.  ", "'\.  ", "'|.  ",
                          "  |  ", "  .|'", "  ./'", "  .-'", "  _.-", "  __.",
                          "  ---", "  --.", "  -._", "  -..", "  '\.", "  '|."  ]
                    rad= self.d[k]
                    deg= int(rad*180/PI)
                    symno= int(.5+ (rad+PI) / (PI/12) )
                    symno= symno % (len(asyms)-1)
                    strout= '%5.2f %3d (%s)' % (rad,deg,asyms[symno])
                elif k == 'skid': # A sensible interpretation of wheel spin.
                    frontwheelradpersec= self.d['wheelSpinVel'][0]
                    skid= 0
                    if frontwheelradpersec:
                        skid= .5555555555*self.d['speedX']/frontwheelradpersec - .66124
                    strout= bargraph(skid,-.05,.4,50,'*')
                elif k == 'slip': # A sensible interpretation of wheel spin.
                    frontwheelradpersec= self.d['wheelSpinVel'][0]
                    slip= 0
                    if frontwheelradpersec:
                        slip= ((self.d['wheelSpinVel'][2]+self.d['wheelSpinVel'][3]) -
                              (self.d['wheelSpinVel'][0]+self.d['wheelSpinVel'][1]))
                    strout= bargraph(slip,-5,150,50,'@')
                else:
                    strout= str(self.d[k])
            out+= "%s: %s\n" % (k,strout)
        return out

class DriverAction():
    '''What the driver is intending to do (i.e. send to the server).
    Composes something like this for the server:
    (accel 1)(brake 0)(gear 1)(steer 0)(clutch 0)(focus 0)(meta 0) or
    (accel 1)(brake 0)(gear 1)(steer 0)(clutch 0)(focus -90 -45 0 45 90)(meta 0)'''
    def __init__(self):
       self.actionstr= str()
       # "d" is for data dictionary.
       self.d= { 'accel':0.2,
                   'brake':0,
                  'clutch':0,
                    'gear':1,
                   'steer':0,
                   'focus':[-90,-45,0,45,90],
                    'meta':0
                    }

    def clip_to_limits(self):
        """There pretty much is never a reason to send the server
        something like (steer 9483.323). This comes up all the time
        and it's probably just more sensible to always clip it than to
        worry about when to. The "clip" command is still a snakeoil
        utility function, but it should be used only for non standard
        things or non obvious limits (limit the steering to the left,
        for example). For normal limits, simply don't worry about it."""
        self.d['steer']= clip(self.d['steer'], -1, 1)
        self.d['brake']= clip(self.d['brake'], 0, 1)
        self.d['accel']= clip(self.d['accel'], 0, 1)
        self.d['clutch']= clip(self.d['clutch'], 0, 1)
        if self.d['gear'] not in [-1, 0, 1, 2, 3, 4, 5, 6]:
            self.d['gear']= 0
        if self.d['meta'] not in [0,1]:
            self.d['meta']= 0
        if type(self.d['focus']) is not list or min(self.d['focus'])<-180 or max(self.d['focus'])>180:
            self.d['focus']= 0

    def __repr__(self):
        self.clip_to_limits()
        out= str()
        for k in self.d:
            out+= '('+k+' '
            v= self.d[k]
            if not type(v) is list:
                out+= '%.3f' % v
            else:
                out+= ' '.join([str(x) for x in v])
            out+= ')'
        return out
        return out+'\n'

    def fancyout(self):
        '''Specialty output for useful monitoring of bot's effectors.'''
        out= str()
        od= self.d.copy()
        od.pop('gear','') # Not interesting.
        od.pop('meta','') # Not interesting.
        od.pop('focus','') # Not interesting. Yet.
        for k in sorted(od):
            if k == 'clutch' or k == 'brake' or k == 'accel':
                strout=''
                strout= '%6.3f %s' % (od[k], bargraph(od[k],0,1,50,k[0].upper()))
            elif k == 'steer': # Reverse the graph to make sense.
                strout= '%6.3f %s' % (od[k], bargraph(od[k]*-1,-1,1,50,'S'))
            else:
                strout= str(od[k])
            out+= "%s: %s\n" % (k,strout)
        return out

# == Misc Utility Functions
def destringify(s):
    '''makes a string into a value or a list of strings into a list of
    values (if possible)'''
    if not s: return s
    if type(s) is str:
        try:
            return float(s)
        except ValueError:
            print("Could not find a value in %s" % s)
            return s
    elif type(s) is list:
        if len(s) < 2:
            return destringify(s[0])
        else:
            return [destringify(i) for i in s]

class TelemetryLogger():
    '''Writes one CSV row per simulation step and collects a short run
    summary (max speed, first off-track, first damage, laps). Used only
    when the client is started with --log; the protocol part is untouched.'''
    columns= ['step', 'curLapTime', 'lastLapTime', 'distFromStart',
              'distRaced', 'speedX', 'speedY', 'trackPos', 'angle',
              'track_ahead', 'track_min', 'rpm', 'gear', 'damage',
              'steer', 'accel', 'brake', 'gear_cmd']

    def __init__(self, path):
        self.path= path
        folder= os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        self.f= open(path, 'w', newline='', encoding='utf-8')
        self.w= csv.writer(self.f)
        self.w.writerow(self.columns)
        self.closed= False
        # Summary state.
        self.steps= 0
        self.last= None             # Last logged sensor snapshot (dict).
        self.max_speed= None        # (speedX, distFromStart, curLapTime)
        self.first_offtrack= None   # (distFromStart, speedX, curLapTime, step)
        self.offtrack_steps= 0
        self.offtrack_events= 0     # Number of separate excursions.
        self.was_offtrack= False
        self.first_damage= None     # (distFromStart, speedX, curLapTime, step, damage)
        self.prev_damage= None
        self.prev_last_lap= None
        self.laps= []               # Completed lap times, s.
        self.params_note= None      # Optional one-line driver params description.

    def log(self, S, R):
        '''S = server state dict (c.S.d), R = driver action dict (c.R.d).'''
        self.steps+= 1
        track= S.get('track')
        if type(track) is list and len(track) > LOG_TRACK_AHEAD_IDX:
            track_ahead= track[LOG_TRACK_AHEAD_IDX]
            track_min= min(track)
        else:
            track_ahead, track_min= '', ''
        cur_lap= S.get('curLapTime', 0)
        last_lap= S.get('lastLapTime', 0)
        dfs= S.get('distFromStart', 0)
        speed= S.get('speedX', 0)
        tpos= S.get('trackPos', 0)
        damage= S.get('damage', 0)
        self.w.writerow([self.steps, cur_lap, last_lap, dfs,
                         S.get('distRaced', ''), speed, S.get('speedY', ''),
                         tpos, S.get('angle', ''), track_ahead, track_min,
                         S.get('rpm', ''), S.get('gear', ''), damage,
                         R.get('steer', ''), R.get('accel', ''),
                         R.get('brake', ''), R.get('gear', '')])
        if self.steps % LOG_FLUSH_EVERY == 0:
            self.f.flush()
        # -- Summary bookkeeping --
        self.last= dict(S)
        if self.max_speed is None or speed > self.max_speed[0]:
            self.max_speed= (speed, dfs, cur_lap)
        off= abs(tpos) > OFFTRACK_TRACKPOS
        if off:
            self.offtrack_steps+= 1
            if not self.was_offtrack:
                self.offtrack_events+= 1
            if self.first_offtrack is None:
                self.first_offtrack= (dfs, speed, cur_lap, self.steps)
        self.was_offtrack= off
        if self.prev_damage is not None and damage > self.prev_damage:
            if self.first_damage is None:
                self.first_damage= (dfs, speed, cur_lap, self.steps, damage)
        self.prev_damage= damage
        # A lap is completed when lastLapTime changes to a new positive value.
        if self.prev_last_lap is not None and last_lap != self.prev_last_lap and last_lap > 0:
            self.laps.append(last_lap)
        self.prev_last_lap= last_lap

    def summary(self):
        out= ['===== Run summary (%s) =====' % self.path]
        if self.params_note:
            out.append('params:           %s' % self.params_note)
        out.append('steps logged:     %d (~%.1f s)' % (self.steps, self.steps / 50.0))
        if not self.steps:
            out.append('no data received from server.')
            return '\n'.join(out)
        L= self.last
        out.append('distRaced total:  %.1f m' % L.get('distRaced', 0))
        out.append('last position:    distFromStart %.1f m, curLapTime %.2f s'
                   % (L.get('distFromStart', 0), L.get('curLapTime', 0)))
        out.append('max speedX:       %.1f km/h at distFromStart %.1f m (curLapTime %.2f s)'
                   % self.max_speed)
        if self.first_offtrack:
            out.append('first off-track:  distFromStart %.1f m, speedX %.1f km/h, curLapTime %.2f s (step %d)'
                       % self.first_offtrack)
        else:
            out.append('first off-track:  none')
        out.append('off-track steps:  %d (%d excursions, |trackPos| > %.2f)'
                   % (self.offtrack_steps, self.offtrack_events, OFFTRACK_TRACKPOS))
        if self.first_damage:
            out.append('first damage:     distFromStart %.1f m, speedX %.1f km/h, curLapTime %.2f s (step %d, damage %.0f)'
                       % self.first_damage)
        else:
            out.append('first damage:     none')
        out.append('total damage:     %.0f' % L.get('damage', 0))
        if self.laps:
            out.append('laps completed:   %d -> %s' % (len(self.laps),
                       ', '.join(['%.3f s' % t for t in self.laps])))
            out.append('best lap:         %.3f s' % min(self.laps))
        else:
            out.append('laps completed:   0')
        return '\n'.join(out)

    def close(self):
        '''Flush and close the CSV, print the summary. Safe to call twice.'''
        if self.closed: return
        self.closed= True
        try:
            self.f.flush()
            self.f.close()
        finally:
            print(self.summary())

def drive_example(c, params=None):
    '''Rule-based driver (spec 0008): target speed from the range finders
    (braking-distance physics), proportional throttle/brake with ABS and
    traction control, steering from angle/trackPos, rpm-based gears,
    off-track and stuck recovery. All tunables come from `params`
    (default: c.params, set by --params, or DEFAULT_PARAMS).'''
    S,R= c.S.d,c.R.d
    P= params if params is not None else getattr(c, 'params', DEFAULT_PARAMS)
    st= c.__dict__.setdefault('_drive_state', {'stuck': 0, 'reverse': 0})
    speed= S['speedX']          # km/h
    angle= S['angle']           # rad, > 0 -> track axis is to the left
    tpos= S['trackPos']         # > 0 -> left of the centre line
    track= S['track']           # 19 range finders, m (-1 when off track)
    wsv= S['wheelSpinVel']      # rad/s: FR, FL, RR, RL

    # -- Stuck detection -> drive in reverse for a while --
    if st['reverse'] <= 0:
        if abs(speed) < P['stuck_speed'] and abs(angle) > P['stuck_angle']:
            st['stuck']+= 1
        else:
            st['stuck']= 0
        if st['stuck'] > int(P['stuck_steps']):
            st['stuck']= 0
            st['reverse']= int(P['reverse_steps'])
    if st['reverse'] > 0:
        st['reverse']-= 1
        R['gear']= -1
        R['steer']= clip(-P['reverse_steer_gain'] * angle, -1, 1)
        R['accel']= clip(P['reverse_accel'], 0, 1)
        R['brake']= 0
        return

    # -- Target speed from the free distance ahead --
    mid= len(track) // 2
    k= int(clip(round(P['ahead_halfwidth']), 0, mid))
    d_ahead= max(track[mid-k:mid+k+1])
    ray_deg= 0.0  # Direction of the longest ray (ties -> closest to straight ahead).
    if len(track) == len(TRACK_SENSOR_ANGLES):
        i_long= max(range(len(track)),
                    key=lambda i: (track[i], -abs(TRACK_SENSOR_ANGLES[i])))
        ray_deg= TRACK_SENSOR_ANGLES[i_long]
    v_c= P['v_corner'] / 3.6                       # m/s
    v2= v_c*v_c + 2*P['decel']*max(0.0, d_ahead - P['dist_margin'])
    v_target= math.sqrt(max(0.0, v2)) * 3.6        # km/h
    v_target-= P['curv_speed_gain'] * abs(ray_deg)
    v_target= clip(v_target, P['v_min'], P['v_max'])
    offtrack= abs(tpos) > 1.0
    if offtrack:
        v_target= min(v_target, P['offtrack_speed'])

    # -- Steering --
    pos_gain= P['offtrack_pos_gain'] if offtrack else P['steer_pos_gain']
    steer= P['steer_angle_gain']*angle - pos_gain*tpos
    if not offtrack:
        # Negative sensor angle = left, positive steer = left.
        steer-= P['steer_ray_gain'] * math.radians(ray_deg)
    R['steer']= clip(steer, -1, 1)

    # -- Throttle / brake --
    err= v_target - speed
    if err < -P['brake_hyst']:
        accel= 0.0
        brake= clip(P['brake_gain'] * (-err), 0, 1)
    else:
        accel= clip(P['accel_gain'] * (err + P['brake_hyst']), 0, 1)
        brake= 0.0
    v_ms= speed / 3.6
    rf, rr= P['wheel_radius_front'], P['wheel_radius_rear']
    # ABS: wheel slower than the car -> locking -> release brake.
    if brake > 0 and speed > max(P['abs_min_speed'], 1.0):
        radii= [rf, rf, rr, rr]
        slip= max([(v_ms - wsv[i]*radii[i]) / v_ms for i in range(4)])
        if slip > P['abs_slip']:
            brake*= clip(1 - P['abs_gain']*(slip - P['abs_slip']), 0, 1)
    # TCS: driven (rear) wheels faster than the car -> cut throttle.
    if accel > 0 and speed > max(P['tcs_min_speed'], 1.0):
        slip= ((wsv[2] + wsv[3]) / 2 * rr - v_ms) / v_ms
        if slip > P['tcs_slip']:
            accel*= clip(1 - P['tcs_gain']*(slip - P['tcs_slip']), 0, 1)
    R['accel']= accel
    R['brake']= brake

    # -- Automatic transmission by rpm --
    gear= int(S['gear'])
    gear_max= int(clip(round(P['gear_max']), 1, 6))
    if gear < 1:
        gear= 1
    elif S['rpm'] > P['gear_up_rpm'] and gear < gear_max:
        gear+= 1
    elif S['rpm'] < P['gear_down_rpm'] and gear > 1:
        gear-= 1
    R['gear']= min(gear, gear_max)
    return

# ================ MAIN ================
if __name__ == "__main__":
    C= Client(p=3001)
    params_src= C.paramsFile if C.paramsFile else 'defaults'
    print('params (%s): %s' % (params_src, json.dumps(C.params, sort_keys=True)))
    logger= TelemetryLogger(C.logFile) if C.logFile else None
    if logger is not None:
        logger.params_note= params_src
    try:
        for step in range(C.maxSteps,0,-1):
            C.get_servers_input()
            drive_example(C)
            C.respond_to_server()
            # After a server shutdown C.so is None and S.d is stale: skip it.
            if logger is not None and C.so is not None:
                logger.log(C.S.d, C.R.d)
    finally:
        # Runs on normal end, server shutdown and Ctrl+C alike.
        if logger is not None:
            logger.close()
    C.shutdown()
