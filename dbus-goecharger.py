#!/usr/bin/env python
import dbus 
import atexit 
import math
# import normal packages
import platform 
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests # for http GET
import configparser # for config/ini file
 
# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService


class DbusGoeChargerService:
  def __init__(self, servicename, paths, productname='go-eCharger', connection='go-eCharger HTTP JSON service'):
    config = self._getConfig()
    
    self._getLoggingLevel(config)
    
    deviceinstance = int(config['DEFAULT']['Deviceinstance'])
    hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
    guiname = config['DEFAULT']['Name'];
    self._SetL1 = config['DEFAULT'].getint('PhaseL1',1);
    self._SwitchL2L3 = config['DEFAULT'].getboolean('SwitchL1L2',False);
    
    self._dbusservice = VeDbusService("{}.http_{:02d}".format(servicename, deviceinstance),register=False)
    self._paths = paths
    
    logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))
    '''
    paths_wo_unit = [
      '/Status'  # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
      '/Mode'
    ]
    '''
    #get data from go-eCharger
    data = self._getGoeChargerData()

    # Create the management objects, as specified in the ccgx dbus-api document
    self._dbusservice.add_path('/Mgmt/ProcessName', __file__)
    self._dbusservice.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self._dbusservice.add_path('/Mgmt/Connection', connection)
    
    # Create the mandatory objects
    self._dbusservice.add_path('/DeviceInstance', deviceinstance)
    self._dbusservice.add_path('/ProductId', 0xFFFF) # 
    self._dbusservice.add_path('/ProductName', productname)
    self._dbusservice.add_path('/CustomName', guiname)    
    if data:
       self._dbusservice.add_path('/FirmwareVersion', int(data['fwv'].replace('.', '')))
       self._dbusservice.add_path('/Serial', data['sse'])
    self._dbusservice.add_path('/HardwareVersion', hardwareVersion)
    self._dbusservice.add_path('/Connected', 1)
    self._dbusservice.add_path('/UpdateIndex', 0)
   
    self._dbusservice.add_path('/Position',1)
 
    # add paths without units
    '''
    for path in paths_wo_unit:
      self._dbusservice.add_path(path, None)
    '''
    
    # add path values to dbus
    for path, settings in self._paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    self._dbusservice.register()

    bus = dbus.SystemBus()
    #bus.get_object('com.victronenergy.system', '/Ac/In/0/Servicename')
    self._powerGrid = bus.get_object('com.victronenergy.grid.mymeter', '/Ac/Power')
    self._powerBattery = bus.get_object('com.victronenergy.vebus.ttyS4', '/Dc/0/Power')
    self._powerBatteryMaxCharge = bus.get_object('com.victronenergy.settings', '/Settings/CGwacs/MaxChargePower')
    self._powerBatteryMaxCharge_reset = self._powerBatteryMaxCharge.GetValue()
    self._powerBatteryMaxCharge_last = self._powerBatteryMaxCharge_reset;
    self._powerBatteryExt = bus.get_object('com.victronenergy.acsystem.VartaElement', '/Ac/In/1/P')
    
    #Charge/Invert Internal/External Battery
    self._maxPowerUnloadBattery = 0
    self._maxPowerUnloadBatteryExt = 0
    
    #Invert Internal/External Battery during Loading Car
    self._maxPowerUnloadBatteryDuringCharging = 0
    self._maxPowerUnloadBatteryDuringChargingExt = 0
    
    #
    self._powerWallboxAvg = 0
    self._powerOverloadAvg = 0
    self._powerUnderloadAvg = 0
    
    self._pvOverloadCount = 0
    self._pvUnderloadCount = 0
    self._pvCurrentCount = 0;
    self._lastNumberOfPhases = 3
    self._waitForDisconnect = False
    
    self._powerBatteryAvg = 0
    self._powerBatteryExtAvg = 0
    
    self._batteryReduceloadCount = 0
    self._batteryIncreaseloadCount = 0
    self._batteryCount = 0
    
    # last update
    self._lastUpdate = 0
    
    # charging time in float
    self._chargingTime = 0.0
    
    self._statusMessage = ""

    # add _update function 'timer'
    gobject.timeout_add(1000, self._update) # pause 250ms before the next request
    
    # add _signOfLife 'timer' to get feedback in log every 5minutes
    gobject.timeout_add(self._getSignOfLifeInterval()*60*1000, self._signOfLife)
 
  def _getConfig(self):
    config = configparser.ConfigParser(strict=False)
    config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
    return config
 
  def _getLoggingLevel(self, config):
    print(config['DEFAULT']['LOG_level'])
    log_level_info = {'logging.DEBUG': logging.DEBUG, 
                        'logging.INFO': logging.INFO,
                        'logging.WARNING': logging.WARNING,
                        'logging.ERROR': logging.ERROR,
                        }
    my_log_level_from_config = config['DEFAULT']['Log_Level']
    my_log_level = log_level_info.get(my_log_level_from_config, logging.ERROR)
    logging.getLogger().setLevel(my_log_level)
    
  def _getSignOfLifeInterval(self):
    config = self._getConfig()
    value = config['DEFAULT']['SignOfLifeLog']
    
    if not value: 
        value = 0
    
    return int(value)
  
  
  def _getGoeChargerStatusUrl(self):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s/status" % (config['ONPREMISE']['Host'])
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
  
  def _getGoeChargerMqttPayloadUrl(self, parameter, value):
    config = self._getConfig()
    accessType = config['DEFAULT']['AccessType']
    
    if accessType == 'OnPremise': 
        URL = "http://%s/mqtt?payload=%s=%s" % (config['ONPREMISE']['Host'], parameter, value)
    else:
        raise ValueError("AccessType %s is not supported" % (config['DEFAULT']['AccessType']))
    
    return URL
  
  def _setGoeChargerValue(self, parameter, value):
    print("_setGoeChargerValue ",parameter,"=",value)
    URL = self._getGoeChargerMqttPayloadUrl(parameter, str(value))
    request_data = requests.get(url = URL)
    
    # check for response
    if not request_data:
      raise ConnectionError("No response from go-eCharger - %s" % (URL))
    
    json_data = request_data.json()
    
    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")
    
    if json_data[parameter] == str(value):
      return True
    else:
      logging.warning("go-eCharger parameter %s not set to %s" % (parameter, str(value)))
      return False
    
 
  def _getGoeChargerData(self):
    URL = self._getGoeChargerStatusUrl()
    try:
       request_data = requests.get(url = URL, timeout=5)
    except Exception:
       return None
    
    # check for response
    if not request_data:
        raise ConnectionError("No response from go-eCharger - %s" % (URL))
    
    json_data = request_data.json()     
    
    # check for Json
    if not json_data:
        raise ValueError("Converting response to JSON failed")
    
    
    return json_data
 
 
  def _signOfLife(self):
    logging.info("--- Start: sign of life ---")
    logging.info("Last _update() call: %s" % (self._lastUpdate))
    logging.info("Last '/Ac/Power': %s" % (self._dbusservice['/Ac/Power']))
    logging.info("Last '/Mode': %s" % (self._dbusservice['/Mode']))
    logging.info("Last '/SetCurrent': %s" % (self._dbusservice['/SetCurrent']))
    logging.info("Last 'statusMessage': %s" % (self._statusMessage))
    logging.info("--- End: sign of life ---")
    return True
  
  def _evCharger2GoeMode(self, mode):
    if mode==0:
        return 0
    elif mode==1:
        return 1
    elif mode==2:
        return 4
    else:
        return 0    
  
  def _goeMode2EvCharger(self, mode):
    #print("_goeMode2EvCharger(",mode,")")
    if mode==0:
        return 0
    elif mode==4:
        return 2
    else:
        return 1
        
  def reset(self):
     logging.info("BATT::Reset maxCharge= %s W",self._powerBatteryMaxCharge_reset)
     self._powerBatteryMaxCharge.SetValue(self._powerBatteryMaxCharge_reset)
  
  def _batterySetLoad(self, power, maxPower):
    if power<0:
       power = 0
    if power>self._powerBatteryMaxCharge_reset:
       power = self._powerBatteryMaxCharge_reset
    #print("_batterySetLoad power=",power," W")
    
    if power!=maxPower:
        #print("_batterySetLoad SetValue->",power)
        self._powerBatteryMaxCharge_last = power
        self._powerBatteryMaxCharge.SetValue(power)
        
  def _pvSetLoad(self, current, currentMax):
    #print("_pvSetLoad(",current," A, ",currentMax," A)")
    if current==0:
       if self._dbusservice['/StartStop']==1:
          logging.info("Wallbox::Stop Loading")   
          self._dbusservice['/StartStop'] = 0
          self._setGoeChargerValue('alw', 0)
       if currentMax>0 and self._dbusservice['/SetCurrent']!=currentMax:
          logging.info("Wallbox::Reset current to %s A" % (currentMax))
          self._dbusservice['/SetCurrent'] = currentMax   
          self._setGoeChargerValue('amp', currentMax)
    else:
       if currentMax > 0 and current>currentMax:
          current = currentMax
          
       if self._dbusservice['/SetCurrent']!=current:
          logging.info("Wallbox::Set current to %s A" % (current))
          self._dbusservice['/SetCurrent'] = current   
          self._setGoeChargerValue('amp', current)
       if self._dbusservice['/StartStop']==0:
          logging.info("Wallbox::Start Loading") 
          self._dbusservice['/StartStop'] = 1  
          self._setGoeChargerValue('alw', 1)
  
  def _getNumberOfPhases(self, powerL1, powerL2, powerL3):
        count = 0;
        
        if powerL1>0: 
            count = count+1
        if powerL2>0: 
            count = count+1
        if powerL3>0: 
            count = count+1
        
        return count
	
  def getPowerWallbox(self, current): 
    return current*230*self._lastNumberOfPhases
	
	
  def getPowerWallboxUp(self, current): 
    return -1.0*(self.getPowerWallbox(current)+200)

  def getPowerWallboxDown(self, current): 
    return -1.0*(self.getPowerWallbox(current)-200)

  def _updatePVsurplusCharging(self, powerGrid, powerWallbox, powerBattery, powerBatteryExt, current, maxCurrent):
    border = 30
    debug = True
    power = powerGrid - powerWallbox
		
    if powerBattery < -self._maxPowerUnloadBatteryDuringCharging:
    	power = power + (self._maxPowerUnloadBatteryDuringCharging - powerBattery)
    if powerBatteryExt < -self._maxPowerUnloadBatteryDuringChargingExt:
    	power = power + (self._maxPowerUnloadBatteryDuringChargingExt - powerBatteryExt)

    logging.debug("WALLBOX::updatePVsurplus _pvCount= %s - %s power = %s W (up %s W, down %s W) wallbox = %s W battery = %s W (%s W) batteryExt = %s W (%s W)",self._pvOverloadCount,self._pvUnderloadCount,power,self.getPowerWallboxUp(current),self.getPowerWallboxDown(current),powerWallbox, powerBattery,self._maxPowerUnloadBatteryDuringCharging,powerBatteryExt,self._maxPowerUnloadBatteryDuringChargingExt)

    logging.debug("WALLBOX::updatePVsurplus _curreentCount = %s wallboxAvg = %s W",self._pvCurrentCount,self._powerWallboxAvg)
    
    if self._pvCurrentCount>=border:
        self._pvCurrentCount = 0;
        if self._powerWallboxAvg==0:
            current = 6;
        elif self._powerWallboxAvg<500 and current>0:
            #Stop Load and wait for disconnect
            self._waitForDisconnect = True
            return 0
    else:
        self._powerWallboxAvg = (self._powerWallboxAvg * self._pvCurrentCount +powerWallbox)/(self._pvCurrentCount +1)
        self._pvCurrentCount = self._pvCurrentCount +1
        
    newCurrent = current
    

    if self.getPowerWallboxUp(newCurrent)> power:
    	self._powerOverloadAvg = (self._powerOverloadAvg*self._pvOverloadCount + power)/(self._pvOverloadCount + 1);
    	self._pvUnderloadCount = 0
    	self._pvOverloadCount = self._pvOverloadCount + 1
    elif self.getPowerWallboxDown(newCurrent)< power:
        self._powerUnderloadAvg = (self._powerUnderloadAvg*self._pvUnderloadCount + power)/(self._pvUnderloadCount + 1);
        self._pvOverloadCount = 0
        if powerWallbox>0:
            self._pvUnderloadCount = self._pvUnderloadCount + 1
        else:
            self._pvUnderloadCount = 0
    else:
    	self._pvUnderloadCount = 0
    	self._pvOverloadCount = 0

        
    if self._pvOverloadCount>=border:
    	self._pvOverloadCount = 0		
    	while newCurrent<maxCurrent and self.getPowerWallboxUp(newCurrent)> self._powerOverloadAvg:			
    		newCurrent = newCurrent+1

    	if debug: 
    		print("UP ",self.getPowerWallboxUp(newCurrent)," W > ",power," W")

    if self._pvUnderloadCount>=border:
    	self._pvUnderloadCount = 0	
    	while newCurrent>6 and self.getPowerWallboxDown(newCurrent)< self._powerUnderloadAvg:
    		newCurrent = newCurrent-1

    	if debug: 
    		print("Down ",self.getPowerWallboxDown(newCurrent)," W < ",power," W")

    if debug: 
    	print("=> ",current," A -> ",newCurrent," A")

    #if newCurrent!=current:
    #	self._pvSetLoad(newCurrent, maxCurrent)
    return newCurrent

  def _updateBattery(self, powerBattery, powerBatteryExt, powerBatteryMaxCharge):
    border = 15
    borderZeroBattery = 100
    #debug = False
    
    logging.debug("BATT::Update _batterCount= %s BatteryAvg = %s W BatteryExtAvg = %s W Battery = %s W BatteryExt = %s W",self._batteryCount,int(self._powerBatteryAvg),int(self._powerBatteryExtAvg), int(powerBattery),int(powerBatteryExt));
       
    
    if self._batteryCount>=border:
        self._batteryCount = 0;
        if self._powerBatteryAvg > borderZeroBattery and self._powerBatteryExtAvg<self._maxPowerUnloadBatteryExt:
            #Reduce Load
            value = round((self._powerBatteryAvg + (self._powerBatteryExtAvg))/100+0.5,0)*100
            logging.info("BATT::[batteryAvg=%s W batteryExtAvg=%s W]\tReduce max. charge rate to %s W",int(self._powerBatteryAvg), int(self._powerBatteryExtAvg),(value))
            self._batterySetLoad(value, powerBatteryMaxCharge)
        elif self._powerBatteryExtAvg>=0 and powerBatteryMaxCharge<self._powerBatteryMaxCharge_reset:
            #Reset Load when Ext Battery Charging
            #if powerBatteryExt > 1000:
            logging.info("BATT::[batteryAvg=%s W batteryExtAvg=%s W]\tIncrease max. charge rate to max by %s W",int(self._powerBatteryAvg), int(self._powerBatteryExtAvg),(self._powerBatteryMaxCharge_reset))
            self._batterySetLoad(self._powerBatteryMaxCharge_reset, powerBatteryMaxCharge)
            #else
            #value = powerBattery-(gridPower+100)
            #value = int(math.ceil(value / 100.0)) * 100
            #logging.info("Increase max. charge rate to %s W" % (value))
            #self._batterySetLoad(value, powerBatteryMaxCharge)
        else:
            logging.info("BATT::[batteryAvg=%s W batteryExtAvg=%s W]\tNo Action",int(self._powerBatteryAvg), int(self._powerBatteryExtAvg))
    else:
        self._powerBatteryAvg = (self._powerBatteryAvg * self._batteryCount +powerBattery)/(self._batteryCount +1)
        self._powerBatteryExtAvg = (self._powerBatteryExtAvg * self._batteryCount +powerBatteryExt)/(self._batteryCount +1)
        self._batteryCount = self._batteryCount +1

  def _updatePV(self, status, mode, powerGrid, powerWallbox, powerBattery, powerBatteryExt, current, maxCurrent): 
  	border = 60
  	
  	logging.debug("WALLBOX::Update Status = %s Mode = %s External-StartStop = %s Current = %s A",status,mode,self._dbusservice['/ExternalStartStop'],self._dbusservice['/ExternalSetCurrent'])
	
	# Car is connected
  	if status==3 and self._dbusservice['/ExternalStartStop']==0: #Charging finished
  		self._statusMessage = "[Status=3] Charging finished";
    
  		self._pvUnderloadCount = 0
  		self._pvOverloadCount = 0
  		#self._pvSetLoad(0, maxCurrent)
  		if self._dbusservice['/ExternalStartStop']==0:
  			self._pvSetLoad(0, maxCurrent)
  		#elif self._dbusservice['/ExternalSetCurrent']==0:					
  		#	self._pvSetLoad(0, maxCurrent)
  	elif status!=0:
  		if mode==0:
  			self._statusMessage = "[Status = "+str(status)+"] Mode=0";
  			logging.debug("WALLBOX::Manual Mode")
  		elif mode==1:
  			logging.debug("WALLBOX::Auto Mode")
  			self._statusMessage = "[Status = "+str(status)+"] Mode=1";
  			newCurrent = self._updatePVsurplusCharging(powerGrid, powerWallbox, powerBattery, powerBatteryExt, current, maxCurrent)
               
  			'''
			power = powerGrid - powerWallbox

			if powerBattery < -self._maxPowerUnloadBatteryDuringCharging:
				power = power + (self._maxPowerUnloadBatteryDuringCharging - powerBattery)
			if powerBatteryExt < -self._maxPowerUnloadBatteryDuringChargingExt:
				power = power + (self._maxPowerUnloadBatteryDuringChargingExt - powerBatteryExt)

			if debug: 
				print("Update _pvCount=",self._pvOverloadCount,"-",self._pvUnderloadCount," W power=",power," W (up ",self.getPowerWallboxUp(current)," down ",self.getPowerWallboxDown(current),") powerWallbox=",powerWallbox," W battery ", powerBattery, " W (",self._maxPowerUnloadBatteryDuringCharging," W) ext ",powerBatteryExt," W (",self._maxPowerUnloadBatteryDuringChargingExt," W)")

			newCurrent = current

			if self.getPowerWallboxUp(newCurrent)> power:
				self._pvUnderloadCount = 0
				self._pvOverloadCount = self._pvOverloadCount + 1
			elif self.getPowerWallboxDown(newCurrent)< power:
				self._pvOverloadCount = 0
				if powerWallbox>0:
					self._pvUnderloadCount = self._pvUnderloadCount + 1
				else:
					self._pvUnderloadCount = 0
			else:
				self._pvUnderloadCount = 0
				self._pvOverloadCount = 0

			if self._pvOverloadCount>=border:
				self._pvOverloadCount = 0		
				while newCurrent<maxCurrent and self.getPowerWallboxUp(newCurrent)> power:			
					newCurrent = newCurrent+1

				if debug: 
					print("UP ",self.getPowerWallboxUp(newCurrent)," W > ",power," W")

			if self._pvUnderloadCount>=border:
				self._pvUnderloadCount = 0	
				while newCurrent>6 and self.getPowerWallboxDown(newCurrent)< power:
					newCurrent = newCurrent-1

				if debug: 
					print("Down ",self.getPowerWallboxDown(newCurrent)," W < ",power," W")

			if debug: 
				print("=> ",current," A -> ",newCurrent," A")
  			'''		  
  			if newCurrent!=current:
  				#print("Set New Current")
  				if self._dbusservice['/ExternalStartStop']==0:
  					#print("AB")
  					self._pvSetLoad(newCurrent, maxCurrent)
  				elif self._dbusservice['/ExternalSetCurrent']==0:	
  					#print("ABC")
  					if newCurrent>0:
  						#print("ABCD")
  						self._pvSetLoad(newCurrent, maxCurrent)
  				#else:
  					#print("ABEE ",self._dbusservice['/ExternalStartStop']," and ",self._dbusservice['/ExternalSetCurrent'])
  		elif mode==2:
  			self._statusMessage = "[Status = "+str(status)+"] Mode=2";
  			logging.debug("WALLBOX::Plan Mode")
		
  	else:
  		self._statusMessage = "[Status=0] No Car";
        
  		self._pvUnderloadCount = 0
  		self._pvOverloadCount = 0
  		self._pvSetLoad(0, maxCurrent)
		
  	logging.debug("WALLBOX::Update ... end")
			
  def _update(self): 
    
    try:
       debug = True
       debugBattery = False
       #get data from go-eCharger
       data = self._getGoeChargerData()
       
       gridPower = self._powerGrid.GetValue()
       powerBattery = self._powerBattery.GetValue()
       powerBatteryExt = self._powerBatteryExt.GetValue()
       powerBatteryMaxCharge = self._powerBatteryMaxCharge.GetValue()
       
       
       #Check if maxCharge is changed external
       if powerBatteryMaxCharge!=self._powerBatteryMaxCharge_reset and powerBatteryMaxCharge!=self._powerBatteryMaxCharge_last:
          self._powerBatteryMaxCharge_reset = powerBatteryMaxCharge
          logging.info("Set max. charge value by reset to %s W" % (powerBatteryMaxCharge))
       
       #Action
       self._updateBattery(powerBattery, powerBatteryExt, powerBatteryMaxCharge)
		  
       
       if data is not None:
          #send data to DBus
          powerL1 = int(data['nrg'][7] * 0.1 * 1000)
          powerL2 = int(data['nrg'][8] * 0.1 * 1000)
          powerL3 = int(data['nrg'][9] * 0.1 * 1000)
          if self._SetL1==2:
             self._dbusservice['/Ac/L1/Power'] = powerL2
             if self._SwitchL2L3==True:
                self._dbusservice['/Ac/L2/Power'] = powerL1
                self._dbusservice['/Ac/L3/Power'] = powerL3
             else:
                self._dbusservice['/Ac/L2/Power'] = powerL3
                self._dbusservice['/Ac/L3/Power'] = powerL1
          elif self._SetL1==3:
             self._dbusservice['/Ac/L1/Power'] = powerL3
             if self._SwitchL2L3==True:
                self._dbusservice['/Ac/L2/Power'] = powerL1
                self._dbusservice['/Ac/L3/Power'] = powerL2
             else:
                self._dbusservice['/Ac/L2/Power'] = powerL2
                self._dbusservice['/Ac/L3/Power'] = powerL1
          else:
             self._dbusservice['/Ac/L1/Power'] = powerL1
             if self._SwitchL2L3==True:
                self._dbusservice['/Ac/L2/Power'] = powerL3
                self._dbusservice['/Ac/L3/Power'] = powerL2
             else:
                self._dbusservice['/Ac/L2/Power'] = powerL2
                self._dbusservice['/Ac/L3/Power'] = powerL3
          
          numberOfPhase = self._getNumberOfPhases(powerL1,powerL2,powerL3)
          if numberOfPhase!=0 and numberOfPhase!=self._lastNumberOfPhases:
            logging.info("Detect number of phases of %s" % (numberOfPhase))
            self._lastNumberOfPhases = numberOfPhase;
          
          powerWallbox = int(data['nrg'][11] * 0.01 * 1000)
          self._dbusservice['/Ac/Power'] = powerWallbox
          self._dbusservice['/Ac/Voltage'] = int(data['nrg'][0])
          self._dbusservice['/Current'] = max(data['nrg'][4] * 0.1, data['nrg'][5] * 0.1, data['nrg'][6] * 0.1)
          self._dbusservice['/Ac/Energy/Forward'] = int(float(data['eto']) / 10.0)
          
          current = int(data['amp'])
          if current!=self._dbusservice['/SetCurrent']:
            self._dbusservice['/SetCurrent'] = current                
            if self._lastUpdate>0:
                logging.info("External changed SetCurrent to %s [goe-App/Wallbox]" % (current))   
                self._dbusservice['/ExternalSetCurrent'] = current 
                
          maxCurrent = int(data['ama']) 
          self._dbusservice['/MaxCurrent'] = maxCurrent
          
          startStop = int(data['alw'])
          if startStop!=self._dbusservice['/StartStop']:
            self._dbusservice['/StartStop'] = startStop
            if self._lastUpdate>0:       
                logging.info("External changed StartStop to %s [goe-App/Wallbox]" % (startStop))            
                self._dbusservice['/ExternalStartStop'] = startStop
                if startStop==0:
                    self._dbusservice['/ExternalSetCurrent'] = 0
          
          # update chargingTime, increment charge time only on active charging (2), reset when no car connected (1)
          timeDelta = time.time() - self._lastUpdate
          if int(data['car']) == 2 and self._lastUpdate > 0:  # vehicle loads
            self._chargingTime += timeDelta
          elif int(data['car']) == 1:  # charging station ready, no vehicle
            self._chargingTime = 0
          self._dbusservice['/ChargingTime'] = int(self._chargingTime)
          #print("_dbusservice['/Mode']",self._dbusservice['/Mode'] )
          mode = self._goeMode2EvCharger(int(data['ast']))  # Manual, no control
          self._dbusservice['/Mode'] = mode
          
          config = self._getConfig()
          hardwareVersion = int(config['DEFAULT']['HardwareVersion'])
          if hardwareVersion == 3:
            self._dbusservice['/MCU/Temperature'] = int(data['tma'][0])
          else:
            self._dbusservice['/MCU/Temperature'] = int(data['tmp'])

          # value 'car' 1: charging station ready, no vehicle 2: vehicle loads 3: Waiting for vehicle 4: Charge finished, vehicle still connected
          status = 0
          if int(data['car']) == 1:
            status = 0
          elif int(data['car']) == 2:
            status = 2
          elif int(data['car']) == 3:
            status = 6
          elif int(data['car']) == 4:
            status = 3
          self._dbusservice['/Status'] = status

          
          #action
          self._updatePV(status, mode,  gridPower, powerWallbox, powerBattery, powerBatteryExt, current, maxCurrent)
                  
          
                

          #logging
          logging.debug("Wallbox Consumption (/Ac/Power): %s" % (self._dbusservice['/Ac/Power']))
          logging.debug("Wallbox Forward (/Ac/Energy/Forward): %s" % (self._dbusservice['/Ac/Energy/Forward']))
          logging.debug("---")
          
          # increment UpdateIndex - to show that new data is available
          index = self._dbusservice['/UpdateIndex'] + 1  # increment index
          if index > 255:   # maximum value of the index
            index = 0       # overflow from 255 to 0
          self._dbusservice['/UpdateIndex'] = index

          #update lastupdate vars
          self._lastUpdate = time.time()  
       else:
          logging.debug("Wallbox is not available")

    except Exception as e:
       logging.critical('Error at %s', '_update', exc_info=e)
       
    # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
    return True
 
  def _handlechangedvalue(self, path, value):
    #logging.info("someone else updated %s to %s" % (path, value))
    #print("_handlechangedvalue ",path, "=",value)
    if path == '/SetCurrent':
      logging.info("External changed SetCurrent to %s [VRM]" % (value))
      self._dbusservice['/ExternalSetCurrent'] = value 
      return self._setGoeChargerValue('amp', value)
    elif path == '/StartStop':
      logging.info("External changed StartStop to %s [VRM]" % (value))
      self._dbusservice['/ExternalStartStop'] = value
      if value==0:
      	self._dbusservice['/ExternalSetCurrent'] = 0
      return self._setGoeChargerValue('alw', value)
    elif path == '/MaxCurrent':
      return self._setGoeChargerValue('ama', value)
    elif path == '/Mode':
      logging.info("External changed Mode to %s [VRM]" % (value))
      return self._setGoeChargerValue('ast', self._evCharger2GoeMode(value))    
    else:
      logging.info("mapping for evcharger path %s does not exist" % (path))
      return False

def end(service):
  service.reset()
  print('Goodbye');

def main():
  #configure logging
  '''
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            filemode='w', 
                            level=logging.INFO,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
  '''
	#filename=("%s/goeCharger_Vorne.log" % (os.path.dirname(os.path.realpath(__file__)))), 
  logging.basicConfig(format='%(asctime)s %(levelname)-8s %(message)s',
                        datefmt='%Y-%m-%d %H:%M:%S', 
					    #filemode='w',                        
                        level=logging.INFO,
                            handlers=[
                                logging.FileHandler("%s/goeCharger_Vorne.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
  try:
      logging.info("Start")
  
      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
     
      #formatting 
      _kwh = lambda p, v: (str(round(v, 2)) + 'kWh')
      _a = lambda p, v: (str(round(v, 1)) + 'A')
      _w = lambda p, v: (str(round(v, 1)) + 'W')
      _v = lambda p, v: (str(round(v, 1)) + 'V')
      _degC = lambda p, v: (str(v) + '°C')
      _s = lambda p, v: (str(v) + 's')
      _n = lambda p, v: (str(v))
     
      #start our main-service
      pvac_output = DbusGoeChargerService(
        servicename='com.victronenergy.evcharger',
        paths={
          '/Ac/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L1/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L2/Power': {'initial': 0, 'textformat': _w},
          '/Ac/L3/Power': {'initial': 0, 'textformat': _w},
          '/Ac/Energy/Forward': {'initial': 0, 'textformat': _kwh},
          '/ChargingTime': {'initial': 0, 'textformat': _s},
          
          '/Ac/Voltage': {'initial': 0, 'textformat': _v},
          '/Current': {'initial': 0, 'textformat': _a},
          '/SetCurrent': {'initial': 0, 'textformat': _a},
          '/ExternalSetCurrent': {'initial': 0, 'textformat': _a},
          '/MaxCurrent': {'initial': 0, 'textformat': _a},
          '/MCU/Temperature': {'initial': 0, 'textformat': _degC},
          '/StartStop': {'initial': 0, 'textformat': _n},
          '/ExternalStartStop': {'initial': 0, 'textformat': _n},			
          '/Mode':  {'initial': 0, 'textformat': _n},
          '/Status':  {'initial': 0, 'textformat': _n},
        }
        )
      atexit.register(end, pvac_output)
      
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      mainloop.run()            
  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)
if __name__ == "__main__":
  main()
