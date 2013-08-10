#!/usr/bin/env python 
# *-* coding: UTF-8 *-*

# Psyrcd the Psybernetics IRC server.
# Based on hircd.py. Modifications have been added for robustness and privacy.
# Gratitude to Ferry Boender for starting this off
# http://www.electricmonk.nl/log/2009/09/14/hircd-minimal-irc-server-in-python/
 
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated documentation
# files (the "Software"), to deal in the Software without
# restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following
# conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT
# HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY,
# WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
# OTHER DEALINGS IN THE SOFTWARE.

# For the best results please use PyPy.
 
# Todo:
#   - Fix the [SSL] disconnect traceback.
#   - Make +e/b conform to the supported_modes{} principle
#   - Create a comparison function for cmode +e/b
#   - Implement MAX_* et al (probably on IRCClient.__init__)
#   - Add a K:Line system
#   - Implement ALL modes.
#   - Check the PID file on startup. Issue a warning and raise SystemExit if psyrcd is already running.
#   - Unicode all the things. (bans and re are particularly tricky and important)
#   - Give scripts more stateful information. IE render(self,file,params)
#   - Alter IRCClient.handle() to first try IRCOperator.handle_* if self.oper. could redefine PRIVMSG for cmode:X
#   - Determine the most elegant way of doing simultanious 6667/6697 operation. (fire off ssl bind() in background thread)
#   - Grep and fix TODO comments.
#   - Add the missing WHOIS response lines.
# Known Errors:
#   - Some commands (/chghost, possibly /kick et al) treat nicks as case-sensitive. (always iterate and compare against nick.lower())
#   - Doesn't daemonize on Windows.
#   - Use $ ./psyrcd --restart to rehash
#   - starting server when already started doesn't work properly. PID file is not changed, no error messsage is displayed.
#   - KeyError(<IRCClient nick!user@addr) (Happens with mirc on nick collisions)
#   - After the server has closed a client connection we will sometimes receive a non-fatal traceback upon writing to the nonexistant socket.
# Server linking:
#   - Determine the most elegant way of performing pathfinding on a branched network. (Requires state on who has what [CONSPIRACY TO MAKE PSYRCD FAT])
#   - Connect through the front door and negotiate as a server, hand connection off to dedicated class.
#   - /operserv connect server:port key; generate key at runtime.
#   - Hook .broadcast(). Higher-level container metaclass/decorator. Serialize/unserialize objects over the wire (why tho?)
# Pipe dreams:
#   - Script threading, long-lived scripts and scheduling.
#   - Script designation based on opership.
#   - An IRC bot which can conjoin external channels on different servers to local channels.
#   - LOCK: Pickle a user or channel object to sqlite3 for later reinsertion. This could form the basis of *serv services.
#   - NickServ and ChanServ (nick registration through smtplib..) [CONSPIRACY TO MAKE PSYRCD FAT]
#   - Logging to sqlite3 (imagine an /operserv replay command for replaying conversations back into a channel)
#   - IPC to external applications [CONSPIRACY TO MAKE PSYRCD FAT]

import sys, os, re, time, optparse, logging, hashlib, SocketServer, socket, select

try:
    from OpenSSL import SSL
except ImportError:
    SSL = None

try:
    from mako.lookup import TemplateLookup
except ImportError:
    TemplateLookup = None

NET_NAME        = "psyrcd-devel"
SRV_VERSION     = "psyrcd-0.13"
SRV_DOMAIN      = "irc.psybernetics.org.uk"
SRV_DESCRIPTION = "I fought the lol and. The lol won."
SRV_WELCOME     = "Welcome to %s" % NET_NAME
SRV_CREATED     = time.asctime()

MAX_CLIENTS   = 300     # User connections to be permitted before we start denying new connections.
MAX_IDLE      = 300     # Time in seconds a user may be caught being idle for.
MAX_NICKLEN   = 12      # Characters per available nickname.
MAX_CHANNELS  = 200     # Channels per server on the network.
MAX_TOPICLEN  = 512     # Characters per channel topic.
MAX_TICKS     = [0,15]  # select()s through active connections before we start pruning for ping timeouts

OPER_USERNAME = os.environ['USER']
OPER_PASSWORD = True    # Set to True to generate a random password, False to disable the oper system, a string of your choice or pipe one at runtime:
                        # openssl rand -base64 32 | ./psyrcd -flVa0.0.0.0

RPL_WELCOME           = '001'
RPL_YOURHOST          = '002'
RPL_CREATED           = '003'
RPL_MYINFO            = '004'
RPL_ISUPPORT          = '005'
RPL_UMODEIS           = '221'
RPL_LUSEROP           = '252'
RPL_LUSERCHANNELS     = '254'
RPL_LUSERME           = '255'
RPL_WHOISUSER         = '311'
RPL_WHOISSERVER       = '312'
RPL_WHOISOPERATOR     = '313'
RPL_WHOISIDLE         = '317'
RPL_ENDOFWHOIS        = '318'
RPL_WHOISCHANNELS     = '319'
RPL_WHOISSPECIAL      = '320'
RPL_LISTSTART         = '321'
RPL_LIST              = '322'
RPL_LISTEND           = '323'
RPL_TOPIC             = '332'
RPL_TOPICWHOTIME      = '333'
RPL_WHOISBOT          = '335'
RPL_INVITING          = '341'
RPL_EXCEPTLIST        = '348'
RPL_ENDOFEXCEPTLIST   = '349'
RPL_WHOREPLY          = '352'
RPL_BANLIST           = '367'
RPL_ENDOFBANLIST      = '368'
RPL_HOSTHIDDEN        = '396'
ERR_NOSUCHNICK        = '401'
ERR_NOSUCHCHANNEL     = '403'
ERR_CANNOTSENDTOCHAN  = '404'
ERR_UNKNOWNCOMMAND    = '421'
ERR_ERRONEUSNICKNAME  = '432'
ERR_NICKNAMEINUSE     = '433'
ERR_NOTIMPLEMENTED    = '449'
ERR_NEEDMOREPARAMS    = '461'
ERR_INVITEONLYCHAN    = '473'
ERR_BANNEDFROMCHAN    = '474'
ERR_CHANOPPRIVSNEEDED = '482'
ERR_VOICENEEDED       = '489'

class IRCError(Exception):
    """
    Exception thrown by IRC command handlers to notify client of a server/client error.
    """
    def __init__(self, code, value):
        self.code = code
        self.value = value

    def __str__(self):
        return repr(self.value)

class IRCChannel(object):
    """
    Object representing an IRC channel.
    """
    def __init__(self, name, topic=''):
        self.name = name
        self.topic_by = name
        self.topic_time = str(time.time())[:10]
        self.topic = topic
        self.clients = set()
        self.supported_modes = {  # Uppercase modes can only be set and removed by opers.
        'A':"Administrators only.",
        'h':"Hide channel operators.",
        'i':"Invite only.",
        'm':"Muted. Only +v and +o users may speak.",
        'n':"No messages allowed from users who are not in the channel.",
        'O':"Operators only.",
        'p':"Private. Hides channel from /whois.",
        'R':"[redacted] Redacts usernames and replaces them with the first word in this line.", # supported_modes['R'].split()[0]
        's':"Secret. Hides channel from /list.",
        't':"Only ops may set the channel topic.",
#        'X':"Executable. Opers can execute code serverside from within the channel"
        }
        self.modes = ['n','t']
        self.ops = {'o':[],'v':[]}
        self.invites = []
        self.bans = [] # '$mask_regex $setter_nick $unix_time' -> i.split()[0]
        self.excepts = []

class IRCOperator(object):
    """
    Object holding stateful info and commands relevant to policing the server from inside.
    """
    def __init__(self,client):
        self.client = client    # So we can access everything relavent to this oper
        self.vhost = "network.admin"
        self.modes = ['A','C','P','Q','S','W']
        self.passwd = None

    def dispatch(self,params):
        """
        Handler for IRCop specific commands.
        """
        try:
            response = ''
            if ' ' in params:
                command, params = params.split(' ', 1)
                handler = getattr(self, 'handle_%s' % (command.lower()))
            else:
                handler = getattr(self, 'handle_%s' % (params.lower()))
            if not handler:
                logging.info('No handler for OPERSERV command: %s.')
                return(': No such operserv command.')
            response = handler(params)
            if response:
                return response
        except Exception, e:
            return('Internal Error: %s' % e)

    def handle_seval(self, params):
        """
        BAD IDEA
        """
        message = ': %s' % (eval(params))
        return(message)

    def handle_dump(self, params):
        """
        Dump internal server info for debugging.
        """
        # TODO: Different arguments for different stats.
        # TODO: Show modes, invites, excepts, bans.
        response = ':%s NOTICE %s :Clients: %s' % (SRV_DOMAIN, self.client.nick, self.client.server.clients)
        self.client.broadcast(self.client.nick,response)
        for client in self.client.server.clients.values():
            response = ':%s NOTICE %s :  %s' % (SRV_DOMAIN, self.client.nick, client)
            self.client.broadcast(self.client.nick,response)
            for channel in client.channels.values():
                response = ':%s NOTICE %s :    %s' % (SRV_DOMAIN, self.client.nick, channel.name)
                self.client.broadcast(self.client.nick,response)
        response = ':%s NOTICE %s :Channels: %s' % (SRV_DOMAIN, self.client.nick, self.client.server.channels)
        self.client.broadcast(self.client.nick,response)
        for channel in self.client.server.channels.values():
            response = ':%s NOTICE %s :  %s %s' % (SRV_DOMAIN, self.client.nick, channel.name, channel)
            self.client.broadcast(self.client.nick,response)
            for client in channel.clients:
                response = ':%s NOTICE %s :    %s %s' % (SRV_DOMAIN, self.client.nick, client.nick, client)
                self.client.broadcast(self.client.nick,response)

    def handle_addoper(self,params):
        """
        Handles adding another serverwide oper.
        Usage: /operserv addoper oper_name passwd
        """
        nick, password = params.split(' ',1)
        user = self.client.server.clients.get(nick)
        if not user:
            return (':%s NOTICE %s : Invalid user.' % (SRV_DOMAIN, self.client.nick))
        self.client.server.opers[user.nick] = IRCOperator(user)
        oper = self.client.server.opers.get(user.nick)
        if password:
            oper.password = password
        response = ':%s NOTICE %s :Created an oper account for %s.' % (SRV_DOMAIN, self.client.nick, user.nick)
        self.client.broadcast(self.client.nick,response)

    def handle_flood(self, params):
        """
        Flood a channel with a given text file.
        """
        channel, file = params.split(' ', 1)
        if os.path.exists(file):
            fd = open(file)
            for line in fd:
                message = ':%s PRIVMSG %s %s' % (self.client.client_ident(), channel, line.strip('\n'))
                self.client.broadcast(channel,message)
        else:
            response = ':%s NOTICE %s :%s does not exist.' % (SRV_DOMAIN, self.client.nick, file)
            self.client.broadcast(self.nick,response)

class IRCClient(SocketServer.BaseRequestHandler):
    """
    IRC client connect and command handling. Client connection is handled by
    the `handle` method which sets up a two-way communication with the client.
    It then handles commands sent by the client by dispatching them to the
    handle_ methods.
    """
    def __init__(self, request, client_address, server):
        self.connected_at = str(time.time())[:10] 
        self.last_activity = 0                    # Subtract this from time.time() to determine idle time.
        self.user = None                          # The bit before the @
        self.host = client_address                # Client's hostname / ip.
        self.rhost = lookup(self.host[0])         # This users rdns. May return None.
        self.hostmask = 'psyrcd' +'-'+hashlib.new('sha512', self.host[0]).hexdigest()[:len(self.host[0])]
        self.realname = None                      # Client's real name
        self.nick = None                          # Client's currently registered nickname
        self.vhost = None                         # Alternative hostmask for WHOIS requests
        self.send_queue = []                      # Messages to send to client (strings)
        self.channels = {}                        # Channels the client is in
        self.modes = ['x']                        # Usermodes set on the client
        self.oper = None                          # Assign an IRCOperator object if user opers up
        self.supported_modes = {                  # Uppercase modes are oper-only
        'A':"IRC Administrator.",
#        'b':"Bot.",
#        'C':"Connection Notices. User receives notices for each connecting and disconnecting client.",
#        'd':"Deaf. User does not recieve channel messages.",
        'H':"Hide ircop line in /whois.",
#        'I':"Invisible. Doesn't appear in /whois, /who, /names, doesn't appear to /join, /part or /quit",
#        'N':"Network Administrator.",
        'O':"IRC Operator.",
#        'P':"Protected. Blocks users from kicking, killing, deoping or devoicing the user.",
#        'p':"Hidden Channels. Hides the channels line in the users /whois",
        'Q':"Kick Block. Cannot be /kicked from channels.",
#        'S':"See Hidden Channels. Allows the IRC operator to see +p and +s channels in /list",
#        'W':"Wallops. Recieves connection, disconnection and traceback notices regarding other users.",
#        'X':"Whois Notification. Allows the IRC operator to see when users /whois him or her.",
        'x':"Masked hostname. Hides the users hostname or IP address from other users."
        }

        SocketServer.BaseRequestHandler.__init__(self, request, client_address, server)
        if options.ssl_key and options.ssl_cert:
            self.connection = self.request
            self.rfile = socket._fileobject(self.request, "rb", self.rbufsize)
            self.wfile = socket._fileobject(self.request, "wb", self.wbufsize)
    def handle(self):
        """
        The nucleus of the IRCd.
        """
        logging.info('Client connected: %s' % (self.client_ident(), ))

        while True:
            buf = ''
            try:
                ready_to_read, ready_to_write, in_error = select.select([self.request], [], [], 0.1)
            except:
                logging.debug('Error sending to nonexistent connection %s' % self.client_ident())
                break

            # Write any commands to the client
            while self.send_queue:
                msg = self.send_queue.pop(0)
                logging.debug('to %s: %s' % (self.client_ident(), msg))
                self.request.send(msg + '\n')

            # See if the client has any commands for us.
            if len(ready_to_read) == 1 and ready_to_read[0] == self.request:
                data = self.request.recv(1024)

                if not data:
                    break
                elif len(data) > 0:
                    # There is data. Process it and turn it into line-oriented input.
                    buf += str(data)

                    while buf.find("\n") != -1:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip()

                        response = ''
                        try:
                            logging.debug('from %s: %s' % (self.client_ident(), line))
                            if ' ' in line:
                                command, params = line.split(' ', 1)
                            else:
                                command = line
                                params = ''
                            if os.path.isfile(scripts_dir + command.lower()) and TemplateLookup:
                                logging.info("%s executing %s script" % (self.nick, command.lower()))
                                # The handler variable must contain something in order to not raise ERR_UNKNOWNCOMMAND
                                handler = render(command.lower(),params) 
                                response = ""
                                for line in handler.split('\n'):
                                    response = response + ":%s NOTICE %s :%s\r\n" % (SRV_DOMAIN, self.nick, line)
                            else:
                                handler = getattr(self, 'handle_%s' % (command.lower()), None)
                            if not handler:
                                logging.info('No handler for command: %s. Full line: %s' % (command, line))
                                raise IRCError(ERR_UNKNOWNCOMMAND, '%s :Unknown command' % (command))
                            if not response:
                                response = handler(params)
                        except AttributeError, e:
#                           self.broadcast('umode:A', self.nick e)
#                           self.broadcast('umode:O', self.nick e)
                            logging.error('%s' % (e))
                        except IRCError, e:
                            response = ':%s %s %s' % (self.server.servername, e.code, e.value)
                            logging.error('%s' % (response))
                        except Exception, e:
#                           self.broadcast('umode:A', self.nick e)
#                           self.broadcast('umode:O', self.nick e)
                            response = ':%s ERROR %s' % (self.server.servername, repr(e))
                            logging.error('%s' % (response))
                        if response:
                            logging.debug('to %s: %s' % (self.client_ident(), response))
                            self.request.send(response + '\r\n')

                        # Ping timeout routine. Every MAX_TICKS[1] rotations of select() incur this routine pruning:
                        if MAX_TICKS[0] >= MAX_TICKS[1]:
                            for client in self.server.clients.values():
                                then = int(client.last_activity)
                                now = int(str(time.time())[:10])
                                if (now - then) > MAX_IDLE:
                                    client.finish(response = ':%s QUIT :Ping timeout. Idle %i seconds.' % (client.client_ident(True), now - then))
                            MAX_TICKS[0] = 0
                        else:
                            MAX_TICKS[0] += 1
        self.request.close()

    def broadcast(self,target,message):
        """
        Handle message dispatch to clients.
        """
        if target.startswith('#'):
            channel = self.server.channels.get(target)
            if channel:
                [client.send_queue.append(message) for client in channel.clients]
        # TODO add 'rhost:*.tld' targets
        elif target.startswith('umode:'):
            umodes = target.split(':')[1]
            for client in self.server.clients.values():
                for mode in umodes:
                    if mode in client.modes:
                        client.send_queue.append(message)
                        break
        elif target.startswith('cmode:'):
            cmodes = target.split(':')[1]
            for channel in self.server.channels.values():
                for mode in cmodes:
                    if mode in channel.modes:
                        for client in channel.clients:
                            client.send_queue.append(message)
                        break
        elif target == '*':
            [client.send_queue.append(message) for client in self.server.clients.values()]
        else:
            client = self.server.clients.get(target)
            if client:
                client.send_queue.append(message)

    def handle_privmsg(self, params):
        """
        Handle sending a private message to a user or channel.
        """
        self.last_activity = str(time.time())[:10] 
        # FIXME: ERR_NEEDMOREPARAMS
        target, msg = params.split(' ', 1)

        message = ':%s PRIVMSG %s %s' % (self.client_ident(), target, msg)
        if target.startswith('#') or target.startswith('$'):
            # Message to channel. Check if the channel exists.
            channel = self.server.channels.get(target)
            if channel:
                if not channel.name in self.channels:
                    # The user isn't in the channel.
                    raise IRCError(ERR_CANNOTSENDTOCHAN, '%s :Cannot send to channel' % (channel.name))
                if 'm' in channel.modes:
                    if self.nick not in channel.ops['o'] and self.nick not in channel.ops['v']:
                        raise IRCError(ERR_VOICENEEDED, '%s :%s is +m.' % (channel.name, channel.name))
                if 'R' in channel.modes:
                    message = ':%s PRIVMSG %s %s' % (channel.supported_modes['R'].split()[0], target, msg)
                for client in channel.clients:
                    if client != self:
                        self.broadcast(client.nick,message)
                # Add a dispatch call here.
            else:
                raise IRCError(ERR_NOSUCHNICK, 'PRIVMSG :%s' % (target))
        else:
            # Message to user
            client = self.server.clients.get(target, None)
            if client:
                self.broadcast(client.nick,message)
            else:
                raise IRCError(ERR_NOSUCHNICK, 'PRIVMSG :%s' % (target))

    def handle_nick(self, params):
        """
        Handle the initial setting of the user's nickname and nick changes.
        """
        nick = params
        # Valid nickname?
        if re.search('[^a-zA-Z0-9\-\[\]\'`^{}_]', nick) or len(nick) > MAX_NICKLEN:
            raise IRCError(ERR_ERRONEUSNICKNAME, ':%s' % (nick))

        # Doesn't overlap with anyone else already here?
        for i in self.server.clients.keys():
            if nick.lower() == i.lower():
                raise IRCError(ERR_NICKNAMEINUSE, 'NICK :%s' % nick)

        if not self.nick:
            # New connection
            self.nick = nick
            self.server.clients[nick] = self
            response = ':%s %s %s :%s' % (self.server.servername, RPL_WELCOME, self.nick, SRV_WELCOME)
            self.broadcast(self.nick,response)
            response = ':%s %s %s :Your host is %s, running version %s' % (self.server.servername, RPL_YOURHOST, self.nick, SRV_DOMAIN, SRV_VERSION)
            self.broadcast(self.nick,response)
            response = ':%s %s %s :This server was created %s' % (self.server.servername,RPL_CREATED,self.nick,SRV_CREATED)
            self.broadcast(self.nick,response)
            # opers, channels, clients and MOTD
            self.handle_lusers(None)
            self.handle_motd(None)
            # Hostmasking
            response = ':%s %s %s %s :is now your displayed host' % (SRV_DOMAIN, RPL_HOSTHIDDEN, self.nick, self.hostmask)
            self.broadcast(self.nick,response)
            response = ':%s MODE %s +x' % (self.client_ident(True), self.nick)
            self.broadcast(self.nick,response)
            return()
        else:
            self.last_activity = str(time.time())[:10] 
            if self.server.clients.get(nick, None) == self:
                # Already registered to user
                return
            else:
                # Nick is available. Change the nick.
                message = ':%s NICK :%s' % (self.client_ident(), nick)

                self.server.clients.pop(self.nick)
                prev_nick = self.nick
                self.nick = nick
                self.server.clients[self.nick] = self 

                # Carry chanops and oper object over.
                for channel_name in self.channels.keys():
                    channel = self.channels.get(channel_name)
                    if prev_nick in channel.ops['o']:
                        channel.ops['o'].remove(prev_nick)
                        channel.ops['o'].append(self.nick)
                    if prev_nick in channel.ops['v']:
                        channel.ops['v'].remove(prev_nick)
                        channel.ops['v'].append(self.nick)
                if self.oper:
                    self.server.opers.pop(prev_nick)
                    self.server.opers[self.nick] = self.oper

                # TODO: Carry channel invites over

                # Send a notification of the nick change to all the clients in
                # the channels the client is in.
                for channel in self.channels.values():
                    for client in channel.clients:
                        if client != self: # do not send to client itself.
                            self.broadcast(client.nick,message)
                # Send a notification of the nick change to the client itself
                self.broadcast(self.nick,message)
                return()

    def handle_user(self, params):
        """
        Handle the USER command which identifies the user to the server.
        """
        if params.count(' ') < 3:
            raise IRCError(ERR_NEEDMOREPARAMS, '%s :Not enough parameters' % (USER))

        user, mode, unused, realname = params.split(' ', 3)
        self.user = user
        self.realname = realname
        if len(self.server.clients) >= MAX_CLIENTS:           # This should be moved to a different section as connections cannot
            self.send_queue.append(': MAX_CLIENTS exceeded.') # be relied upon to be IRC clients.
            self.request.close()
        return('')

    def handle_lusers(self,params):
        """
        Handle the /lusers command
        """
        response = ':%s %s %s %i :operator(s) online' % (self.server.servername, RPL_LUSEROP, self.nick, len(self.server.opers))
        self.broadcast(self.nick,response)
        response = ':%s %s %s %i :channels formed' % (self.server.servername, RPL_LUSERCHANNELS, self.nick, len(self.server.channels))
        self.broadcast(self.nick,response)
        response = ':%s %s %s :I have %i clients' % (self.server.servername, RPL_LUSERME, self.nick, len(self.server.clients))
        self.broadcast(self.nick,response)
        return()

    def handle_motd(self,params):
        if os.path.exists('MOTD'):
            MOTD = open('MOTD')
            for line in MOTD:
                motdline = ":%s 372 %s :- %s" % (SRV_DOMAIN, self.nick, line.strip('\n'))
                self.broadcast(self.nick,motdline)
        else:
            motdline = ":%s 372 %s :- MOTD file missing." % (SRV_DOMAIN, self.nick)
            self.broadcast(self.nick,motdline)
        response = ':%s 376 %s :End of MOTD command.' % (self.server.servername, self.nick)
        self.broadcast(self.nick,response)

    def handle_rules(self,params):
        if os.path.exists('RULES'):
            RULES = open('RULES')
            for line in RULES:
                rulesline = ":%s 232 %s :- %s" % (SRV_DOMAIN, self.nick, line.strip('\n'))
                self.broadcast(self.nick,rulesline)
        else:
            rulesline = ":%s 434 %s :- RULES file missing." % (SRV_DOMAIN, self.nick)
            self.broadcast(self.nick,motdline)
        response = ':%s 376 %s :End of RULES command.' % (self.server.servername, self.nick)
        self.broadcast(self.nick,response)

    def handle_ping(self, params):
        """
        Handle client PING requests to keep the connection alive.
        """
        self.last_activity = str(time.time())[:10] 
        response = ':%s PONG :%s' % (self.server.servername, self.server.servername)
        return (response)

    def handle_join(self, params):
        """
        Handle the JOINing of a user to a channel. Valid channel names start
        with a # and consist of a-z, A-Z, 0-9 and/or '_'.
        """
        self.last_activity = str(time.time())[:10] 
        new_channel = None # Use this to determine if we should make this client an op
        channel_names = params.split(' ', 1)[0] # Ignore keys
        for channel_name in channel_names.split(','):
            r_channel_name = channel_name.strip()

            # Valid channel name?
            if not re.match('^#([a-zA-Z0-9_])+$', r_channel_name):
                raise IRCError(ERR_NOSUCHCHANNEL, '%s :No such channel' % (r_channel_name))

            # Check we're not already there and grab ourselves a channel object
            if r_channel_name not in self.server.channels.keys():
                new_channel = True
                
            channel = self.server.channels.setdefault(r_channel_name, IRCChannel(r_channel_name))

            # Check the channel isn't +i
            if 'i' in channel.modes and self.nick not in channel.invites:
                raise IRCError(ERR_INVITEONLYCHAN, '%s :%s' % (channel.name,channel.name))

            # Check the channel isn't +OA
            if ('O' in channel.modes and not self.oper) or ('A' in channel.modes and not self.oper):
                raise IRCError(500, '%s :Must be an IRC operator' % channel.name)

            # Respect channel bans and exceptions
            if not self.oper:
                for b in channel.bans:
                    for e in channel.excepts:
                        if re.match(e.split()[0],self.client_ident(True)): break
                    else:
                        if re.match(b.split()[0], self.client_ident(True)):
                            raise IRCError(ERR_BANNEDFROMCHAN, '%s :Cannot join channel (+b)' % channel.name)
                        continue  # executed if the loop ended normally (no break)
                    break  # executed if 'continue' was skipped (break)

            # Add ourself to the channel and the channel to users channel list
            channel.clients.add(self)
            self.channels[channel.name] = channel

            # Send join message to everybody in the channel, including yourself
            response = ':%s JOIN :%s' % (self.client_ident(masking=True), r_channel_name)
            if ('I' in self.modes) or ('R' in channel.modes):
                self.broadcast(self.nick,response)
            else:
                self.broadcast(channel.name,response)

            # Send the topic
            if channel.topic != '':
                response = ':%s %s %s %s :%s' % (SRV_DOMAIN, RPL_TOPIC, self.nick, channel.name, channel.topic)
                self.broadcast(self.nick,response)
                response = ':%s %s %s %s %s %s' % (SRV_DOMAIN, RPL_TOPICWHOTIME, self.nick, channel.name, channel.topic_by, channel.topic_time)
                self.broadcast(self.nick,response)

            # Op this user if it's a new channel, which will show up in /names
            if new_channel: channel.ops['o'].append(self.nick)
            self.handle_names(channel.name)

    def handle_names(self,params):
        if params in self.server.channels.keys():
            channel = self.server.channels.get(params)
            if channel.name in self.channels:
                if 'R' in channel.modes:
                    nicks = [channel.supported_modes['R'].split()[0]]
                else:
                    nicks = [client.nick for client in channel.clients]
                    if 'h' not in channel.modes:
                        o = [i for i in channel.ops['o'] if i in nicks]
                        v = [i for i in channel.ops['v'] if i in nicks]
                        for i in o: nicks.remove(i)
                        for i in v: nicks.remove(i)
                        for i in o: o.remove(i);o.append('@'+i)
                        for i in v: v.remove(i);v.append('+'+i)
                        for i in o: nicks.append(i)
                        for i in v: nicks.rappend(i)
                response = ':%s 353 %s = %s :%s' % (self.server.servername, self.nick, channel.name, ' '.join(nicks))
                self.broadcast(self.nick,response)
                response = ':%s 366 %s %s :End of /NAMES list' % (self.server.servername, self.nick, channel.name)
                self.broadcast(self.nick,response)

    def handle_mode(self, params):
        """
        Handle the MODE command which sets and requests UMODEs and CMODEs
        """
        self.last_activity = str(time.time())[:10] 
#       :nick!user@host MODE (#channel) +mode recipient
        if ' ' in params: # User is attempting to set a mode
            modeline = ''
            argument = None
            target, mode = params.split(' ', 1)
            if ' ' in mode: mode, argument = mode.split(' ',1)
            if target.startswith('#'):
                channel = self.server.channels.get(target)
                if self.nick in channel.ops['o'] or self.oper:
                    if not argument: # Set a mode on a channel.
                        if mode.startswith('+'):
                            for i in mode[1:]:
                                if i in channel.supported_modes.keys():
                                    if i.isupper() and not self.oper: continue
                                    channel.modes.append(i)
                                    modeline=modeline+i
                            if modeline:
                                message = ":%s MODE %s +%s" % (self.client_ident(True), target, modeline)
                                self.broadcast(target,message)
                                return()
                        elif mode.startswith('-'):
                            for i in mode[1:]:
                                if i in channel.modes:
                                    if i.isupper() and not self.oper: continue
                                    channel.modes.remove(i)
                                    modeline=modeline+i
                            if modeline:
                                message = ":%s MODE %s -%s" % (self.client_ident(True), target, modeline)
                                self.broadcast(target,message)
                                return()
                    else: # A mode with arguments. Making someone an op/Bans and Excepts. TODO: +k, +l
                        args = argument.split(' ')
                        if mode.startswith('+'):
                            for i in mode[1:]:
                                for n in args:
                                    if i == 'o' or i == 'v':
                                        if n not in channel.ops[i]:
                                            channel.ops[i].append(n)
                                            modeline+=i
                                            args.remove(n)
                                    elif i == 'b':
                                        n = re_to_irc(n)
                                        channel.bans.append('%s %s %s' % (n, self.nick, str(time.time())[:10]))
                                        modeline+=i
                                    elif i == 'e':
                                        n = re_to_irc(n)
                                        channel.excepts.append('%s %s %s' % (n, self.nick, str(time.time())[:10]))
                                        modeline+=i
                            message = ":%s MODE %s +%s %s" % (self.client_ident(True), target, modeline, argument)
                            self.broadcast(target,message)
                            return()
                        elif mode.startswith('-'):
                            for i in mode[1:]:
                                for n in args:
                                    if i == 'o' or i == 'v':
                                        if n in channel.ops[i]:
                                            channel.ops[i].remove(n)
                                            modeline+=i
                                            args.remove(n)
                                    elif i == 'b':
                                        for entry in channel.bans:
                                            if entry.split()[0] == n:
                                                channel.bans.remove(entry)
                                                modeline+=i
                                                args.remove(n)
                                    elif i == 'e':
                                        for entry in channel.excepts:
                                            if entry.split()[0] == n:
                                                channel.excepts.remove(entry)
                                                modeline+=i
                                                args.remove(n)
                            message = ":%s MODE %s -%s %s" % (self.client_ident(True), target, modeline, argument)
                            self.broadcast(target,message)                
                else:
                    raise IRCError(ERR_CHANOPPRIVSNEEDED, '%s :%s You are not a channel operator.' % (channel.name,channel.name))

                # Retrieving the banlist or the exceptlist:
                if (mode == 'b' or mode == '+b') and not argument:
                    for entry in channel.bans:
                        banline = ":%s %s %s %s %s" % (SRV_DOMAIN, RPL_BANLIST, self.nick, channel.name, entry)
                        self.broadcast(self.nick,banline)
                    response = ":%s %s %s %s :End of Channel Ban List" % (SRV_DOMAIN, RPL_ENDOFBANLIST, self.nick, channel.name)
                    self.broadcast(self.nick,response)

                if (mode == 'e' or mode == '+e') and not argument:
                    for entry in channel.excepts:
                        exceptline = ":%s %s %s %s %s" % (SRV_DOMAIN, RPL_EXCEPTLIST, self.nick, channel.name, entry)
                        self.broadcast(self.nick,exceptline)
                    response = ":%s %s %s %s :End of Channel Exception List" % (SRV_DOMAIN, RPL_ENDOFEXCEPTLIST, self.nick, channel.name)
                    self.broadcast(self.nick,response)

            else: # User modes.
                if (self.nick == target) or self.oper:
                    modeline=''
                    if mode.startswith('+'):
                        for i in mode[1:]:
                            if i in self.supported_modes.keys() and i not in self.modes:
                                if i.isupper() and not self.oper: continue
                                self.modes.append(i)
                                modeline=modeline+i
                        if len(modeline) > 0:
                            response = ':%s MODE %s +%s' % (self.client_ident(True), self.nick, modeline)
                            self.broadcast(self.nick,response)
                    elif mode.startswith('-'):
                        for i in mode[1:]:
                            if i in self.modes:
                                if i.isupper() and not self.oper: continue
                                self.modes.remove(i)
                                modeline=modeline+i
                        if len(modeline) > 0:
                            response = ':%s MODE %s -%s' % (self.client_ident(True), self.nick, modeline)
                            self.broadcast(self.nick,response)
        else: # User is requesting a list of modes
            if params.startswith('#'):
                # Check user is in channel unless oper
                modes=''
                channel = self.server.channels.get(params)
                for i in channel.modes: modes=modes+i
                return(':%s 324 %s %s +%s' % (self.server.servername, self.nick, params, modes))
            else:
                if params == self.nick:
                    modes='+'
                    user = self.server.clients.get(params)
                    if user:
                        for i in user.modes: modes=modes+i
                        if len(modes) > 1:
                            response = ':%s %s %s :%s' % (SRV_DOMAIN, RPL_UMODEIS, self.nick, modes)
                            self.broadcast(self.nick,response)
                        else:
                            return(': No UMODEs set for %s' % params)

    def handle_invite(self, params):
        """
        Handle the invite command.
        """
        self.last_activity = str(time.time())[:10] 
        target, channel = params.strip(':').split(' ',1)
        channel = self.server.channels.get(channel)
        if channel and target in self.server.clients.keys():
            if self.nick in channel.ops['o'] or self.oper:
                channel.invites.append(target)

                response = ':%s %s %s %s %s' % (SRV_DOMAIN, RPL_INVITING, self.nick, target, channel.name)
                self.broadcast(self.nick,response)

                # Tell the channel
                response = ':%s NOTICE @%s :%s invited %s into the channel.' % (SRV_DOMAIN, channel.name, self.nick, target)
                self.broadcast(channel.name,response)

                # Tell the invitee
                response = ':%s INVITE %s :%s' % (self.client_ident(True), target, channel.name)
                self.broadcast(target,response)
            else:
                raise IRCError(ERR_CHANOPPRIVSNEEDED, '%s :%s You are not a channel operator.' % (channel.name,channel.name))

    def handle_knock(self, params):
        self.last_activity = str(time.time())[:10] 
       # Open the door
        channel = self.server.channels.get(params)
        if channel:
            if 'i' in channel.modes and channel.name not in self.channels:
                # Get on the floor
                response = ':%s NOTICE @%s :%s knocked on %s.' % (SRV_DOMAIN, channel.name, self.nick, channel.name)
                self.broadcast(channel.name,response)
                # Everybody walk the dinosaur
                response = ':%s NOTICE %s : Knocked on %s' % (SRV_DOMAIN, self.nick, channel.name)
                self.broadcast(self.nick,response)

    def handle_whois(self, params):
        """
        Handle the whois command.
        """
        self.last_activity = str(time.time())[:10] 
        # TODO: IP Addr, Admin, Oper, Bot lines.
        user = self.server.clients.get(params)
        if user:
            # Userhost line.
            if user.vhost:
                response = ':%s %s %s %s %s %s * %s' % (SRV_DOMAIN, RPL_WHOISUSER, self.nick, user.nick, user.nick, user.vhost, user.realname)
                self.broadcast(self.nick,response)
            else:
                response = ':%s %s %s %s %s %s * %s' % (SRV_DOMAIN, RPL_WHOISUSER, self.nick, user.nick, user.nick, user.hostmask, user.realname)
                self.broadcast(self.nick,response)

            # Channels the user is in. Modify to show op status.
            channels=[]
            for channel in user.channels.values():
                if 'p' not in channel.modes: channels.append(channel.name)
            if channels:
                response = ':%s %s %s %s :%s' % (SRV_DOMAIN, RPL_WHOISCHANNELS, self.nick, user.nick, ' '.join(channels))
                self.broadcast(self.nick,response)

            # Oper info
            if user.oper and 'H' not in user.modes:
                if 'A' in user.modes:
                    response = ':%s %s %s %s :%s is a server admin.' % (SRV_DOMAIN, RPL_WHOISOPERATOR, self.nick, user.nick, user.nick)
                    self.broadcast(self.nick,response)
                if 'O' in user.modes:
                    response = ':%s %s %s %s :%s is a server operator.' % (SRV_DOMAIN, RPL_WHOISOPERATOR, self.nick, user.nick, user.nick)
                    self.broadcast(self.nick,response)

            if self.oper or self.nick == user.nick:
                if user.rhost:
                    response = ':%s %s %s %s %s %s' % (SRV_DOMAIN, RPL_WHOISSPECIAL, self.nick, user.nick, user.rhost, user.host[0])
                    self.broadcast(self.nick,response)
                else:
                    response = ':%s %s %s %s %s %s' % (SRV_DOMAIN, RPL_WHOISSPECIAL, self.nick, user.nick, user.host[0])
                    self.broadcast(self.nick,response)

            # Server info line
            response = ':%s %s %s %s %s :%s' % (SRV_DOMAIN, RPL_WHOISSERVER, self.nick, user.nick, SRV_DOMAIN, SRV_DESCRIPTION)
            self.broadcast(self.nick,response)

            # Idle and connection time.
            idle_time = int(str(time.time())[:10]) - int(user.last_activity)
            response = ':%s %s %s %s %i %s :seconds idle, signon time' % (SRV_DOMAIN, RPL_WHOISIDLE, self.nick, user.nick, idle_time, user.connected_at)
            self.broadcast(self.nick,response)

            # That about wraps 'er up.
            response = ':%s %s %s %s :End of /WHOIS list.' % (SRV_DOMAIN, RPL_ENDOFWHOIS, self.nick, user.nick)
        else:
            raise IRCError(ERR_UNKNOWNCOMMAND, '420 :%s is a cool guy.' % params.split(' ', 1)[0])

    def handle_who(self, params):
        """
        Handle the who command.
        Not currently implemented!
        """
        if self.oper:
            for client in self.server.clients.values():
                response = ':%s %s %s :%s %s' % (SRV_DOMAIN, RPL_WHOREPLY, self.nick, client.nick, client.client_ident())
        else:
            return()

    def handle_topic(self, params):
        """
        Handle a topic command.
        """
        self.last_activity = str(time.time())[:10] 
        if ' ' in params:
            channel_name = params.split(' ', 1)[0]
            topic = params.split(' ', 1)[1].lstrip(':')
        else:
            channel_name = params
            topic = None
        channel = self.server.channels.get(channel_name)
        if not channel:
            raise IRCError(ERR_NOSUCHNICK, 'PRIVMSG :%s' % (channel_name))
        if not channel.name in self.channels:
            # The user isn't in the channel.
            raise IRCError(ERR_CANNOTSENDTOCHAN, '%s :Cannot send to channel' % (channel.name))
        if topic:
            if self.nick in channel.ops['o'] or self.oper:
                if topic == channel.topic: return()
                channel.topic = topic
                channel.topic_by = self.nick
                channel.topic_time = str(time.time())[:10]
                message = ':%s TOPIC %s :%s' % (self.client_ident(), channel_name, channel.topic)
                self.broadcast(channel.name,message)
            else:
                raise IRCError(ERR_CHANOPPRIVSNEEDED, '%s :%s You are not a channel operator.' % (channel.name,channel.name))
        else:
            response = ':%s %s %s %s :%s' % (SRV_DOMAIN, RPL_TOPIC, self.nick, channel.name, channel.topic)
            self.broadcast(self.nick,response)
            response = ':%s %s %s %s %s %s' % (SRV_DOMAIN, RPL_TOPICWHOTIME, self.nick, channel.name, channel.topic_by, channel.topic_time)
            self.broadcast(self.nick,response)

    def handle_part(self, params):
        """
        Handle a client parting from channel(s).
        """
        self.last_activity = str(time.time())[:10] 
        for pchannel in params.split(','):
            if pchannel.strip() in self.channels:
                # Send message to all clients in all channels user is in, and remove the user from the channels.
                channel = self.server.channels.get(pchannel.strip())
                if 'R' not in channel.modes:
                    response = ':%s PART :%s' % (self.client_ident(True), pchannel)
                    self.broadcast(channel.name,response)
                self.channels.pop(pchannel)
                channel.clients.remove(self)
                if len(channel.clients) < 1:
                    self.server.channels.pop(channel.name)
            else:
                response = ':%s 403 %s :%s' % (self.server.servername, pchannel, pchannel)
                self.broadcast(self.nick,response)

    def handle_quit(self, params):
        """
        Handle the client breaking off the connection with a QUIT command.
        """
        response = ':%s QUIT :%s' % (self.client_ident(True), params.lstrip(':'))
        self.finish(response)

    def handle_kick(self,params):
        """
        Implement the kick command
        """
        message=None
        channel, target= params.split(' ',1)
        target, message = target.split(' :',1)

        channel = self.server.channels.get(channel)
        if not channel:
            return(':%s NOTICE %s :No such channel.' % (SRV_DOMAIN, self.nick))
        if not self.oper and self.nick not in channel.ops['o']:
            return(':%s NOTICE %s :You are not a channel operator.' % (SRV_DOMAIN, channel.name))

        target = self.server.clients.get(target)
        if not target:
            return(':%s NOTICE @%s :No such nick.' % (SRV_DOMAIN, channel.name))
        if 'Q' in target.modes:
            return(':%s NOTICE @%s :Cannot kick +Q user %s.' % (SRV_DOMAIN, channel.name, target.nick))

        if message:
            response = ':%s KICK %s %s :%s' % (self.client_ident(True), channel.name, target.nick, message)
        else:
            response = ':%s KICK %s %s :%s' % (self.client_ident(True), channel.name, target.nick, self.nick)
        self.broadcast(channel.name, response)
        target.channels.pop(channel.name)
        channel.clients.remove(target)
        self.last_activity = str(time.time())[:10] 

    def handle_list(self,params):
        """
        Implements the /list command
        """
        self.last_activity = str(time.time())[:10] 
        response = ':%s %s %s Channel :Users  Name' % (SRV_DOMAIN, RPL_LISTSTART, self.nick)
        self.broadcast(self.nick,response)
        for channel in self.server.channels.values():
            if 's' not in channel.modes:
                response = ':%s %s %s %s %i :[+%s] %s' % (SRV_DOMAIN,RPL_LIST,self.nick,channel.name,len(channel.clients),''.join(channel.modes),channel.topic)
                self.broadcast(self.nick,response)
        response = ':%s %s %s :End of /LIST' % (SRV_DOMAIN, RPL_LISTEND, self.nick)
        self.broadcast(self.nick,response)

    def handle_oper(self,params):
        """
        Handle the client authenticating itself as an ircop.
        """
        if OPER_PASSWORD == False:
            raise IRCError(ERR_UNKNOWNCOMMAND, ': OPER system is disabled.')
        else:
            if ' ' in params:
                opername, password = params.split(' ', 1)
                if password == OPER_PASSWORD and opername == OPER_USERNAME:
                    oper = self.server.opers.setdefault(self.nick, IRCOperator(self))
                else:
                    oper = self.server.opers.get(opername)
                    if (not oper) or (oper.password != password): return(':%s NOTICE %s :No O:Lines for your host.' % (SRV_DOMAIN, self.nick))
                    #if oper.password != password: return(':%s NOTICE %s :No O:Lines for your host.' % (SRV_DOMAIN, self.nick))
                self.vhost = oper.vhost
                self.oper = oper
                for i in oper.modes: self.modes.append(i)
                return(':%s NOTICE %s :Auth successful for %s.' % (SRV_DOMAIN,self.nick,opername))
            else:
                return(': Incorrect usage.')

    def handle_operserv(self,params):
        """
        Pass authenticated ircop commands to the IRCOperator dispatcher.
        """
        if self.oper:
            return(self.oper.dispatch(params))
        else:
            return(': OPERSERV is only available to authenticated IRCops.')

    def handle_chghost(self,params):
        if self.oper:
            target, vhost = params.split(' ',1)
            target = self.server.clients.get(target)
            if target:
                target.vhost = vhost
                return(':%s NOTICE %s :Changed the vhost for %s to %s.' % (SRV_DOMAIN,self.nick,target.nick,target.vhost))
            else:
                return(':%s NOTICE %s :Invalid nick: %s.' % (SRV_DOMAIN,self.nick,target))
        else:
            return(':%s NOTICE %s :You must be identified as a server op to use CHGHOST.' % (SRV_DOMAIN,self.nick))

    def handle_kill(self,params):
        nick, reason = params.split(' ',1)
        reason = reason.lstrip(':')
        if self.oper:
            client = self.server.clients.get(nick)
            if client:
                client.finish(response=':%s QUIT :Killed by %s: %s' % (client.client_ident(True), self.nick,reason))

    def handle_helpop(self, params):
        """
        The helpop system provides help on commands and modes.
        Use "/helpop command commandname" for documentation on a given command.
        Use "/helpop cmode modename" for documentation on a given channel mode.
        Use "/helpop umode modename" for documentation on a given user mode.
        """
        if not ' ' in params:
            docs = self.handle_helpop.__doc__.split('\n')
            doc = ''
            for line in docs:
                i = 0
                for character in line:
                    if character == ' ':
                        i += 1
                    else:
                        doc += line[i:] + '\n'
                        break
            message = ": %s" % doc
            self.broadcast(self.nick, message)
            if self.oper:
                message = ': Use "/helpop ocommand commandname" for documentation on a given operserv command.'
                self.broadcast(self.nick, message)
        else:
            (section, topic) = params.split(' ',1)
            if section == "umode":
                if topic in self.supported_modes.keys():
                    message = ": %s help on user mode %s" % (SRV_DOMAIN, topic)
                    self.broadcast(self.nick, message)
                    message = ": %s" % self.supported_modes[topic]
                    self.broadcast(self.nick, message)
            elif section == "command":
                if hasattr(self, "handle_"+topic):
                    message = ": %s help on command %s" % (SRV_DOMAIN, topic.upper())
                    self.broadcast(self.nick, message)
                    command = getattr(self,"handle_"+topic)
                    docs = command.__doc__.split('\n')
                    doc = ''
                    for line in docs:
                        i = 0
                        for character in line:
                            if character == ' ':
                                i += 1
                            else:
                                doc += line[i:] + '\n'
                                break
                    message = ": %s" % doc
                    self.broadcast(self.nick, message)
                else:
                    message = ": Unknown command %s"  % topic.upper()
                    self.broadcast(self.nick, message)
            elif section == "ocommand":
                if self.oper:
                    if hasattr(self.oper, "handle_"+topic):
                        message = ": %s help on operserv command %s" % (SRV_DOMAIN, topic.upper())
                        self.broadcast(self.nick, message)
                        command = getattr(self.oper,"handle_"+topic)
                        docs = command.__doc__.split('\n')
                        doc = ''
                        for line in docs:
                            i = 0
                            for character in line:
                                if character == ' ':
                                    i += 1
                                else:
                                    doc += line[i:] + '\n'
                                    break
                        message = ": %s" % doc
                        self.broadcast(self.nick, message)
                    else:
                        message = ": Unknown operserv command %s"  % topic.upper()
                        self.broadcast(self.nick, message)
                else:
                    message  = ": You must be an IRC Operator to view the ocommand section."
                    self.broadcast(self.nick, message)

    def handle_sajoin(self,params):
        """
        Execute self.handle_join() for someone.
        """
        if self.oper:
            target, channel = params.split()
            victim = self.server.clients.get(target)
            if victim: victim.handle_join(channel)

    def handle_sapart(self,params):
        """
        Execute self.handle_part() for someone.
        """
        if self.oper:
            target, channel = params.split()
            victim = self.server.clients.get(target)
            if victim: victim.handle_part(channel)

    def handle_sjoin(self,params):
        """
        Join the user into a randomly named channel: hashlib.new('sha512', self.hostmask).hexdigest()[len(self.hostmask):]
        +Raipstn. Doesn't show up in /list. /names returns [redacted]. PRIVMSG filters names to [redacted]
        """
        pass

    def client_ident(self,masking=None):
        """
        Return the client identifier as included in many command replies.
        """
        if masking:
            if self.vhost == None:
                return('%s!%s@%s' % (self.nick, self.user, self.hostmask))
            else:
                return('%s!%s@%s' % (self.nick, self.user, self.vhost))
        return('%s!%s@%s' % (self.nick, self.user, self.host[0]))

    def finish(self,response=None):
        """
        The client conection is finished. Do some cleanup to ensure that the
        client doesn't linger around in any channel or the client list, in case
        the client didn't properly close the connection with PART and QUIT.
        """
        logging.info('Client disconnected: %s' % (self.client_ident()))
        if response == None:
            response = ':%s QUIT :EOF from client' % (self.client_ident(True))
        for channel in self.channels.values():
            self.broadcast(channel.name,response)
            channel.clients.remove(self)
            if len(channel.clients) < 1:
                self.server.channels.pop(channel.name)
        try:
            self.server.clients.pop(self.nick)
        except KeyError:
            logging.info('There goes the last client!')
        logging.info('Connection finished: %s' % (self.client_ident()))
        self.request.close()

    def __repr__(self):
        """
        Return a user-readable description of the client
        """
        return('<%s %s!%s@%s (%s)>' % (
            self.__class__.__name__,
            self.nick,
            self.user,
            self.host[0],
            self.realname,
            )
        )

class IRCServer(SocketServer.ThreadingMixIn, SocketServer.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass):
        self.servername = SRV_DOMAIN
        self.channels = {} # Existing channels (IRCChannel instances) by channel name
        self.clients = {}  # Connected clients (IRCClient instances) by nickname
        self.opers = {}    # Authenticated IRCops (IRCOperator instances) by nickname
        SocketServer.TCPServer.__init__(self, server_address, RequestHandlerClass)
        if options.ssl_cert and options.ssl_key:
            self.ctx = SSL.Context(SSL.SSLv23_METHOD)
            self.ctx.use_privatekey_file(options.ssl_key)
            self.ctx.use_certificate_file(options.ssl_cert)
            self.socket = SSL.Connection(self.ctx, socket.socket(socket.AF_INET, socket.SOCK_STREAM))
            self.server_bind()
            self.server_activate()
            logging.info("SSL Enabled.")

    def shutdown_request(self,request):
        request.shutdown()

class Daemon:
    """
    Daemonize the current process (detach it from the console).
    """
    def __init__(self, pidfile):
        # Fork a child and end the parent (detach from parent)
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0) # End parent
        except OSError, e:
            sys.stderr.write("fork #1 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(-2)

        # Change some defaults so the daemon doesn't tie up dirs, etc.
        os.setsid()
        os.umask(0)

        # Fork a child and end parent (so init now owns process)
        try:
            pid = os.fork()
            if pid > 0:
                try: 
                    # TODO: Read the file first and determine is a psyrcd daemon is currently running.
                    f = file(pidfile, 'w')
                    f.write(str(pid))
                    f.close()
                except IOError, e:
                    logging.error(e)
                    sys.stderr.write(repr(e))
                sys.exit(0) # End parent
        except OSError, e:
            sys.stderr.write("fork #2 failed: %d (%s)\n" % (e.errno, e.strerror))
            sys.exit(-2)

        # Close STDIN, STDOUT and STDERR so we don't tie up the controlling terminal
        for fd in (0, 1, 2):
            try:
                os.close(fd)
            except OSError:
                pass
def re_to_irc(r, really=True):
    if not really: # Reverse (displaying)
        r = re.sub('\\\.','.',r)
        r = re.sub('\.\*','*',r)
    else: # Forward (setting)
        r = re.sub('\.','\\\.',r)
        r = re.sub('\*','.*',r)
    return r

def lookup(addr):
    try:
        return socket.gethostbyaddr(addr)[0]
    except:
        return None

def hostmatch(entry,host):
    if (host in entry) or (host == entry):
        return True
    else:
        return False

#class scripts(object):
def render(script_file, params):
    template_lookup = TemplateLookup(directories=[scripts_dir], disable_unicode=True, input_encoding='utf-8')
    template = template_lookup.get_template(script_file)
    output = template.render(params=params)
    result = []
    for line in output.split('\n'):
        if line == '': continue
        else: result.append(line)
    return '\n'.join(result)

class color:
    purple = '\033[95m'
    blue = '\033[94m'
    green = '\033[92m'
    orange = '\033[93m'
    red = '\033[91m'
    end = '\033[0m'
    def disable(self):
        self.purple = ''
        self.blue = ''
        self.green = ''
        self.orange = ''
        self.red = ''
        self.end = ''

if __name__ == "__main__":
    # Parameter parsing
    prog = "psyrcd"
    description = "A nimble IRCd for *NIX."
    epilog = "Using the -k and -c options in conjunction will enable SSL at the expense of plaintext connections. SSL Support is currently an experimental feature."

    parser = optparse.OptionParser(prog=prog,version=SRV_VERSION,description=description,epilog=epilog)
    parser.set_usage(sys.argv[0] + " -a0.0.0.0")

    parser.add_option("--start", dest="start", action="store_true", default=True, help="(default)")
    parser.add_option("--stop", dest="stop", action="store_true", default=False)
    parser.add_option("--restart", dest="restart", action="store_true", default=False)
    parser.add_option("--pidfile", dest="pidfile", action="store", default='psyrcd.pid')
    parser.add_option("--logfile", dest="logfile", action="store", default='psyrcd.log')
    parser.add_option("-a", "--address", dest="listen_address", action="store", default='127.0.0.1')
    parser.add_option("-p", "--port", dest="listen_port", action="store", default='6667')
    parser.add_option("-s", "--ssl-port", dest="ssl_port", action="store", default='6697')
    parser.add_option("-V", "--verbose", dest="verbose", action="store_true", default=False)
    parser.add_option("-l", "--log-stdout", dest="log_stdout", action="store_true")
    parser.add_option("-f", "--foreground", dest="foreground", action="store_true")
    parser.add_option("--scripts-dir", dest="scripts_dir",action="store", default='scripts', help="Directory name relative to the psyrcd executable containing auxillary commands [requires mako packge]")
    parser.add_option("-k", "--key", dest="ssl_key",action="store", default=None, help="Requires --cert")
    parser.add_option("-c", "--cert", dest="ssl_cert",action="store", default=None, help="Requires --key")
    parser.add_option("--ssl-help", dest="ssl_help",action="store_true",default=False)
#    parser.add_option("--link-help", dest="link_help",action="store_true",default=False)
    (options, args) = parser.parse_args()

    if options.ssl_help:
        print """Keys and certs are generated with:
$ %sopenssl genrsa 1024 >%s key%s
$ %sopenssl req -new -x509 -nodes -sha1 -days 365 -key key > %scert%s"""%(color.green,color.orange,color.end,color.green,color.orange,color.end)
        raise SystemExit

    # Logging
    logfile = os.path.join(os.path.realpath(os.path.dirname(sys.argv[0])),options.logfile)
    if options.verbose:
        loglevel = logging.DEBUG
    else:
        loglevel = logging.WARNING

    log = logging.basicConfig(
        level=loglevel,
        format='%(asctime)s:%(levelname)s:%(message)s',
        filename=logfile,
        filemode='a')

    # Handle start/stop/restart commands.
    if options.stop or options.restart:
        pid = None
        try:
            f = file('psyrcd.pid', 'r')
            pid = int(f.readline())
            f.close()
            os.unlink('psyrcd.pid')
        except ValueError, e:
            sys.stderr.write('Error in pid file `psyrcd.pid`. Aborting\n')
            sys.exit(-1)
        except IOError, e:
            pass

        if pid:
            os.kill(pid, 15)
        else:
            sys.stderr.write('psyrcd not running or no PID file found\n')

        if not options.restart:
            sys.exit(0)

    # Check the user isn't trying to use TLS without SSL lib
    if options.ssl_key and options.ssl_cert:
        if not SSL:
            sslerror = """

%sOptional crypto requires the pyOpenSSL suite.%s

Try '%ssudo pip install pyOpenSSL%s' or consult your usual package manager.""" % (color.red,color.end,color.green,color.end)
            raise ImportError(sslerror)
            raise SystemExit

    logging.info("Starting psyrcd")
    logging.debug("Logging to %s" % (logfile))

    if options.log_stdout:
        console = logging.StreamHandler()
        formatter = logging.Formatter('[%(levelname)s] %(message)s')
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG)
        logging.getLogger('').addHandler(console)

    if options.verbose:
        logging.info("We're being verbose")

    if OPER_PASSWORD == True:
        OPER_PASSWORD = hashlib.new('sha512', str(os.urandom(20))).hexdigest()[:20]

    if not sys.stdin.isatty():
        OPER_PASSWORD = sys.stdin.read().strip('\n').split(' ',1)[0]

    # Go into daemon mode
    if not options.foreground:
        print "netadmin login:%s /oper %s %s%s" % (color.green, OPER_USERNAME, OPER_PASSWORD, color.end)
        Daemon(options.pidfile)
    else:
        logging.info("netadmin login:%s /oper %s %s %s" % (color.green, OPER_USERNAME, OPER_PASSWORD, color.end))

    # Set variables for processing script files:
    this_dir = os.path.dirname(os.path.abspath(__file__)) + os.path.sep
    scripts_dir = this_dir + options.scripts_dir + os.path.sep
    if TemplateLookup:
        logging.info("Scripts directory defined as %s" % scripts_dir)

    # Start server
    try:
        ircserver = IRCServer((options.listen_address, int(options.listen_port)), IRCClient)
        logging.info('Starting psyrcd on %s:%s' % (options.listen_address, options.listen_port))
        ircserver.serve_forever()
    except socket.error, e:
        logging.error(repr(e))
        sys.exit(-2)
    except KeyboardInterrupt:
        logging.info('Bye.')
        raise SystemExit